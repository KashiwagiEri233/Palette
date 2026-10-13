import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { Button } from '@/components/ui/button'
import { NativeSelect } from '@/components/ui/native-select'
import { PcMouseIcon, ExpandIcon, CollapseIcon, RefreshIcon } from '@/components/icons'
import type { ControlLease, VmRecord, VmStatus, InputEvent } from '../shared/types'
import { InputQueue, physicalKey, pointerPosition } from '@/lib/input'

export function Console({ vm, reportError }: { vm: VmRecord; reportError: (message: string) => void }) {
  const mount = useRef<HTMLDivElement>(null)
  const panel = useRef<HTMLElement>(null)
  const surface = useRef<HTMLDivElement>(null)
  const rfb = useRef<RFB | null>(null)
  const lease = useRef<ControlLease | null>(null)
  const queue = useRef<InputQueue | null>(null)
  const geometry = useRef({ width: 0, height: 0, epoch: 0 })
  const [connected, setConnected] = useState(false)
  const [controlling, setControlling] = useState(false)
  const [scale, setScale] = useState('fit')
  const [revision, setRevision] = useState(0)
  const [displayError, setDisplayError] = useState('')
  const [fullscreen, setFullscreen] = useState(false)
  const [fullscreenPending, setFullscreenPending] = useState(false)
  const running = vm.status.state === 'RUNNING'

  async function release(): Promise<void> {
    const previous = lease.current
    lease.current = null
    queue.current?.stop(); queue.current = null
    setControlling(false)
    if (previous) {
      try { await window.palette.request('control.release', vm.vm_id, { lease_id: previous.lease_id }) }
      catch (error) { reportError(String(error)) }
    }
  }

  useEffect(() => {
    const syncFullscreen = (): void => setFullscreen(document.fullscreenElement === panel.current)
    const escapeFullscreen = (event: KeyboardEvent): void => {
      if (event.key === 'Escape' && document.fullscreenElement === panel.current) {
        event.preventDefault(); event.stopPropagation()
        void toggleFullscreen()
      }
    }
    syncFullscreen()
    document.addEventListener('fullscreenchange', syncFullscreen)
    document.addEventListener('keydown', escapeFullscreen, true)
    return () => {
      document.removeEventListener('fullscreenchange', syncFullscreen)
      document.removeEventListener('keydown', escapeFullscreen, true)
    }
  }, [])

  async function toggleFullscreen(): Promise<void> {
    setFullscreenPending(true)
    try {
      if (document.fullscreenElement) await document.exitFullscreen()
      else await panel.current?.requestFullscreen()
      surface.current?.focus()
    } catch (error) { reportError(String(error)) }
    finally { setFullscreenPending(false) }
  }

  useEffect(() => {
    let disposed = false
    let observer: MutationObserver | undefined
    setConnected(false); setDisplayError('')
    if (!running || !vm.status.instance_id || !mount.current) return
    const sync = async (): Promise<void> => {
      const result = await window.palette.request('display.sync', vm.vm_id) as { status: VmStatus }
      const display = result.status.display
      if (display) geometry.current = { ...display, epoch: result.status.display_epoch ?? 0 }
    }
    void (async () => {
      try {
        await sync()
        const result = await window.palette.request('display.ticket', vm.vm_id, { instance_id: vm.status.instance_id }) as { url: string }
        if (disposed || !mount.current) return
        const client = new RFB(mount.current, result.url, { shared: true })
        rfb.current = client
        client.viewOnly = true; client.scaleViewport = true; client.resizeSession = false; client.focusOnClick = false
        client.addEventListener('connect', () => { if (!disposed) setConnected(true) })
        client.addEventListener('disconnect', () => {
          if (rfb.current === client) rfb.current = null
          if (disposed) return
          setConnected(false); setDisplayError('画面连接已断开'); void release()
        })
        observer = new MutationObserver(() => {
          const canvas = mount.current?.querySelector('canvas')
          if (canvas && (canvas.width !== geometry.current.width || canvas.height !== geometry.current.height)) {
            void release().then(sync).catch(error => reportError(String(error)))
          }
        })
        observer.observe(mount.current, { childList: true, subtree: true, attributes: true, attributeFilter: ['width', 'height'] })
      } catch (error) { if (!disposed) setDisplayError(String(error)) }
    })()
    const lost = (): void => { void release() }
    const visibility = (): void => { if (document.hidden) lost() }
    const fullscreen = (): void => { if (!document.fullscreenElement) lost() }
    window.addEventListener('blur', lost)
    window.addEventListener('palette-control-lost', lost)
    document.addEventListener('visibilitychange', visibility)
    document.addEventListener('fullscreenchange', fullscreen)
    return () => {
      disposed = true; observer?.disconnect(); void release()
      rfb.current?.disconnect(); rfb.current = null
      window.removeEventListener('blur', lost); window.removeEventListener('palette-control-lost', lost)
      document.removeEventListener('visibilitychange', visibility); document.removeEventListener('fullscreenchange', fullscreen)
    }
  }, [vm.vm_id, vm.status.instance_id, running, revision])

  useEffect(() => {
    const batch = window.setInterval(() => { void queue.current?.flush() }, 16)
    const heartbeat = window.setInterval(() => {
      if (lease.current) void window.palette.request('control.heartbeat', vm.vm_id, { lease_id: lease.current.lease_id }).catch(error => { reportError(String(error)); void release() })
    }, 1000)
    return () => { clearInterval(batch); clearInterval(heartbeat) }
  }, [vm.vm_id])

  async function acquire(): Promise<void> {
    try {
      const result = await window.palette.request('control.acquire', vm.vm_id, { instance_id: vm.status.instance_id }) as ControlLease
      lease.current = result; geometry.current.epoch = result.display_epoch
      queue.current = new InputQueue(async (seq, events) => {
        if (!lease.current) return
        const { width, height, epoch } = geometry.current
        return window.palette.request('control.input', vm.vm_id, { lease_id: lease.current.lease_id, seq, display_epoch: epoch, width, height, events })
      }, error => { reportError(String(error)); void release() })
      setControlling(true); surface.current?.focus()
    } catch (error) { reportError(String(error)) }
  }

  function send(event: InputEvent): void { queue.current?.push(event) }
  function point(event: React.PointerEvent): { x: number; y: number } | null {
    const canvas = mount.current?.querySelector('canvas')
    if (!canvas) return null
    return pointerPosition(canvas.getBoundingClientRect(), geometry.current.width, geometry.current.height, event.clientX, event.clientY)
  }
  function key(event: React.KeyboardEvent, down: boolean): void {
    if (!lease.current || event.target !== surface.current) return
    if (event.ctrlKey && event.altKey && event.code === 'KeyG') { event.preventDefault(); void release(); return }
    const code = physicalKey(event.code)
    if (!code) return
    event.preventDefault(); event.stopPropagation()
    if (!event.repeat) send({ type: 'key', key: code, down })
  }
  function chord(keys: string[]): void {
    for (const key of keys) send({ type: 'key', key, down: true })
    for (const key of [...keys].reverse()) send({ type: 'key', key, down: false })
    surface.current?.focus()
  }

  return <section ref={panel} className="console-panel" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) void release() }}>
    <div className="console-toolbar">
      <div className="flex items-center gap-2"><span className={`status-dot ${connected ? 'online' : ''}`} /><span>{connected ? controlling ? '人工控制' : '仅查看' : '未连接'}</span></div>
      <div className="flex items-center gap-2">
        <Button variant={controlling ? 'default' : 'secondary'} disabled={!connected} onClick={() => controlling ? void release() : void acquire()}><PcMouseIcon className="size-4" />{controlling ? '交还 Agent' : '接管'}</Button>
        <NativeSelect aria-label="画面缩放" value={scale} onChange={event => { setScale(event.target.value); if (rfb.current) rfb.current.scaleViewport = event.target.value === 'fit' }}><option value="fit">适应窗口</option><option value="native">原始尺寸</option></NativeSelect>
        <Button variant="ghost" size="icon" aria-label="重新连接画面" onClick={() => setRevision(value => value + 1)}><RefreshIcon className="size-4" /></Button>
        <Button variant="ghost" size="icon" aria-label={fullscreen ? '退出全屏' : '全屏'} title={fullscreen ? '退出全屏' : '全屏'} aria-pressed={fullscreen} disabled={fullscreenPending} onClick={() => void toggleFullscreen()}>{fullscreen ? <CollapseIcon className="size-4" /> : <ExpandIcon className="size-4" />}</Button>
      </div>
    </div>
    <div ref={surface} className={`console-surface ${controlling ? 'captured' : ''}`} tabIndex={0} aria-label="虚拟机画面" onKeyDown={event => key(event, true)} onKeyUp={event => key(event, false)}
      onContextMenu={event => event.preventDefault()}
      onPointerDownCapture={event => { if (!lease.current) return; const position = point(event); if (!position) return; event.preventDefault(); surface.current?.focus(); event.currentTarget.setPointerCapture(event.pointerId); send({ type: 'move', ...position }); const button = ['left', 'middle', 'right'][event.button]; if (button) send({ type: 'button', button: button as 'left' | 'middle' | 'right', down: true }) }}
      onPointerMoveCapture={event => { if (!lease.current) return; const position = point(event); if (position) send({ type: 'move', ...position }) }}
      onPointerUpCapture={event => { if (!lease.current) return; const button = ['left', 'middle', 'right'][event.button]; if (button) send({ type: 'button', button: button as 'left' | 'middle' | 'right', down: false }); if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId) }}
      onPointerCancel={() => void release()}
      onWheelCapture={event => { if (!lease.current) return; const horizontal = Math.abs(event.deltaX) > Math.abs(event.deltaY); const delta = horizontal ? event.deltaX : event.deltaY; if (delta) send({ type: 'wheel', direction: horizontal ? delta < 0 ? 'left' : 'right' : delta < 0 ? 'up' : 'down', steps: Math.min(8, Math.max(1, Math.round(Math.abs(delta) / 100))) }) }}>
      <div ref={mount} className="rfb-mount" />
      {!connected && <div className="console-placeholder"><p>{displayError || (running ? '正在连接画面…' : '虚拟机尚未启动')}</p></div>}
    </div>
    <div className="console-footer"><span>{geometry.current.width ? `${geometry.current.width} × ${geometry.current.height}` : '—'}</span>{controlling && <div className="flex items-center gap-2"><span>Ctrl + Alt + G 释放控制</span><Button variant="ghost" size="sm" onClick={() => chord(['ctrl', 'alt', 'delete'])}>Ctrl + Alt + Del</Button><Button variant="ghost" size="sm" onClick={() => chord(['meta_l'])}>Super</Button></div>}</div>
  </section>
}
