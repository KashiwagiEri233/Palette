import { CopyDebugInfoButton } from './components/CopyDebugInfoButton'
import { useEffect, useRef, useState } from 'react'
import RFB from '@novnc/novnc'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PcMouseIcon, ExpandIcon, CollapseIcon, RefreshIcon } from '@/components/icons'
import type { ControlLease, VmRecord, VmStatus, InputEvent } from '../shared/types'
import { InputQueue, WheelAccumulator, inputErrorCode, physicalKey, pointerPosition } from '@/lib/input'
import { Play, Power, RotateCw, Monitor, AlertTriangle, MousePointer2, MoreHorizontal } from '@/components/icons/workbench'
import { ButtonGroup } from '@/components/ui/button-group'
import { EmptyState } from '@/components/ui/empty-state'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from '@/components/ui/dropdown-menu'
import { IconButton, activeVm } from './components/workspace-primitives'
import { toast } from 'sonner'
import type { GuestStatus } from '../shared/guest'

export function Console({ vm, reportError, onAction, busy = false }: { vm: VmRecord; reportError: (message: string) => void; onAction?: (action: string) => void; busy?: boolean }) {
  const mount = useRef<HTMLDivElement>(null)
  const panel = useRef<HTMLElement>(null)
  const surface = useRef<HTMLDivElement>(null)
  const rfb = useRef<RFB | null>(null)
  const lease = useRef<ControlLease | null>(null)
  const queue = useRef<InputQueue | null>(null)
  const wheel = useRef(new WheelAccumulator())
  const geometry = useRef({ width: 0, height: 0, epoch: 0 })
  const [connected, setConnected] = useState(false)
  const [controlling, setControlling] = useState(false)
  const [scale, setScale] = useState('fit')
  const selectedScale = useRef('fit')
  const [revision, setRevision] = useState(0)
  const [displayError, setDisplayError] = useState('')
  const [fullscreen, setFullscreen] = useState(false)
  const [fullscreenPending, setFullscreenPending] = useState(false)
  const acquisition = useRef(0)
  const modifierOnly = useRef(false)
  const scaleMenuOpen = useRef(false)
  const [acquiring, setAcquiring] = useState(false)
  const running = vm.status.state === 'RUNNING'
  const shuttingDown = vm.status.state === 'STOPPING'
  const displayActive = running || shuttingDown
  const status = useRef(vm.status)
  status.current = vm.status
  const [guestStatus, setGuestStatus] = useState<GuestStatus | null>(null)
  const [autoResize, setAutoResize] = useState(false)
  const autoResizeRef = useRef(false)
  const resizePending = useRef<{ width: number; height: number; at: number } | null>(null)

  useEffect(() => {
    let disposed = false
    let polling = false
    setGuestStatus(null); setAutoResize(false); autoResizeRef.current = false; resizePending.current = null
    const poll = async (): Promise<void> => {
      if (disposed || polling || status.current.state !== 'RUNNING' || !vm.status.instance_id) return
      polling = true
      try { const result = await window.palette.request('guest.status', vm.vm_id, { instance_id: vm.status.instance_id }) as GuestStatus; if (!disposed) setGuestStatus(result) }
      catch { if (!disposed) setGuestStatus(null) }
      finally { polling = false }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 3000)
    return () => { disposed = true; clearInterval(timer) }
  }, [vm.vm_id, vm.status.instance_id, vm.status.boot_epoch, running])

  useEffect(() => {
    if (!autoResize || !controlling || !guestStatus?.desktop.resize || !surface.current) return
    let timer = 0
    let disposed = false
    const observer = new ResizeObserver(() => {
      clearTimeout(timer)
      timer = window.setTimeout(() => {
        const current = lease.current
        if (!current || disposed || !surface.current) return
        const width = Math.min(4096, Math.max(320, Math.floor(surface.current.clientWidth / 2) * 2))
        const height = Math.min(4096, Math.max(200, Math.floor(surface.current.clientHeight / 2) * 2))
        if (geometry.current.width === width && geometry.current.height === height) return
        const pending = resizePending.current
        if (pending && pending.width === width && pending.height === height) return
        resizePending.current = { width, height, at: Date.now() }
        void window.palette.request('control.display.resize', vm.vm_id, { instance_id: vm.status.instance_id, lease_id: current.lease_id, width, height }).then(() => {
          window.setTimeout(() => { const pending = resizePending.current; if (!disposed && pending && Date.now() - pending.at >= 7500) { resizePending.current = null; setAutoResize(false); autoResizeRef.current = false; toast.info('客户机未确认分辨率变化，已保留画面缩放') } }, 8000)
        }).catch(error => { if (!disposed) { resizePending.current = null; setAutoResize(false); autoResizeRef.current = false; reportError(String(error)) } })
      }, 500)
    })
    observer.observe(surface.current)
    return () => { disposed = true; observer.disconnect(); clearTimeout(timer) }
  }, [autoResize, controlling, guestStatus?.desktop.resize, vm.vm_id, vm.status.instance_id])

  async function release(): Promise<void> {
    acquisition.current += 1
    setAcquiring(false)
    const previous = lease.current
    resizePending.current = null
    lease.current = null
    queue.current?.stop(); queue.current = null
    wheel.current.reset()
    setControlling(false)
    if (previous) {
      try { await window.palette.request('control.release', vm.vm_id, { lease_id: previous.lease_id }) }
      catch (error) { reportError(String(error)) }
    }
  }

  useEffect(() => { if (!running) void release() }, [running])
  useEffect(() => {
    if (lease.current && lease.current.boot_epoch !== vm.status.boot_epoch) void release()
  }, [vm.status.boot_epoch])

  useEffect(() => {
    const syncFullscreen = (): void => setFullscreen(document.fullscreenElement === panel.current)
    const escapeFullscreen = (event: KeyboardEvent): void => {
      if (event.key === 'Escape' && document.fullscreenElement === panel.current && !scaleMenuOpen.current) {
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
    let syncingGeometry = false
    let geometryDirty = false
    setConnected(false); setDisplayError('')
    if (!displayActive || !vm.status.instance_id || !mount.current) return
    const sync = async (): Promise<boolean> => {
      if (disposed || !['RUNNING', 'STOPPING'].includes(status.current.state)) return false
      if (status.current.state === 'STOPPING') {
        const display = status.current.display
        if (display) geometry.current = { ...display, epoch: status.current.display_epoch ?? 0 }
        return true
      }
      const result = await window.palette.request('display.sync', vm.vm_id) as { status: VmStatus }
      if (disposed || !['RUNNING', 'STOPPING'].includes(status.current.state) || !['RUNNING', 'STOPPING'].includes(result.status.state)) return false
      const display = result.status.display
      if (display && !disposed) geometry.current = { ...display, epoch: result.status.display_epoch ?? 0 }
      return true
    }
    void (async () => {
      try {
        if (!await sync() || disposed) return
        const result = await window.palette.request('display.ticket', vm.vm_id, { instance_id: vm.status.instance_id }) as { url: string }
        if (disposed || !mount.current) return
        const client = new RFB(mount.current, result.url, { shared: true })
        rfb.current = client
        client.viewOnly = true; client.scaleViewport = selectedScale.current === 'fit'; client.resizeSession = false; client.focusOnClick = false
        client.addEventListener('connect', () => { if (!disposed) setConnected(true) })
        client.addEventListener('disconnect', () => {
          if (rfb.current === client) rfb.current = null
          if (disposed) return
          setConnected(false); setDisplayError('画面连接已断开'); void release()
        })
        const syncGeometry = async (): Promise<void> => {
          if (syncingGeometry) return
          syncingGeometry = true
          let resume = false
          try {
            while (geometryDirty && !disposed) {
              geometryDirty = false
              const canvas = mount.current?.querySelector('canvas')
              if (!canvas || (canvas.width === geometry.current.width && canvas.height === geometry.current.height)) continue
              const pending = resizePending.current
              resume ||= !!lease.current && autoResizeRef.current && pending?.width === canvas.width && pending?.height === canvas.height
              resizePending.current = null
              await release()
              if (!disposed) await sync()
            }
            if (resume && !disposed && document.hasFocus()) await acquire()
          } catch (error) { reportError(String(error)) }
          finally {
            syncingGeometry = false
            if (geometryDirty && !disposed) void syncGeometry()
          }
        }
        observer = new MutationObserver(() => {
          const canvas = mount.current?.querySelector('canvas')
          if (canvas && (canvas.width !== geometry.current.width || canvas.height !== geometry.current.height)) {
            geometryDirty = true
            void syncGeometry()
          }
        })
        observer.observe(mount.current, { childList: true, subtree: true, attributes: true, attributeFilter: ['width', 'height'] })
      } catch (error) { if (!disposed) setDisplayError(String(error)) }
    })()
    const lost = (): void => { if (lease.current) toast.info('已释放人工控制'); void release() }
    const controlLost = (event: Event): void => {
      const detail = (event as CustomEvent<{ vmId: string; leaseId: string; error?: string } | undefined>).detail
      if (detail && (detail.vmId !== vm.vm_id || detail.leaseId !== lease.current?.lease_id)) return
      if (detail?.error) reportInputFailure(detail.error)
      lost()
    }
    const visibility = (): void => { if (document.hidden) lost() }
    const fullscreen = (): void => { if (!document.fullscreenElement) lost() }
    window.addEventListener('blur', lost)
    window.addEventListener('palette-control-lost', controlLost)
    document.addEventListener('visibilitychange', visibility)
    document.addEventListener('fullscreenchange', fullscreen)
    return () => {
      disposed = true; observer?.disconnect(); void release()
      rfb.current?.disconnect(); rfb.current = null
      window.removeEventListener('blur', lost); window.removeEventListener('palette-control-lost', controlLost)
      document.removeEventListener('visibilitychange', visibility); document.removeEventListener('fullscreenchange', fullscreen)
    }
  }, [vm.vm_id, vm.status.instance_id, displayActive, revision])

  useEffect(() => {
    const heartbeat = window.setInterval(() => {
      if (lease.current) void window.palette.request('control.heartbeat', vm.vm_id, { lease_id: lease.current.lease_id }).catch(error => { reportError(String(error)); void release() })
    }, 1000)
    return () => { clearInterval(heartbeat) }
  }, [vm.vm_id])

  /** Input that only arrived because the guest stopped accepts nothing is not an error.
   *
   * A keystroke that races a shutdown or a pause is refused before it is sent; reporting it
   * as a broken channel would hide the real cause and invite a pointless reconnect.
   */
  function reportInputFailure(error: unknown): void {
    if (inputErrorCode(error) === 'VM_NOT_RUNNING') { toast.info('虚拟机未在运行，本次输入已丢弃'); return }
    reportError(String(error))
  }

  async function acquire(): Promise<void> {
    if (status.current.state !== 'RUNNING' || acquiring || !connected) return
    const attempt = ++acquisition.current
    setAcquiring(true)
    try {
      const result = await window.palette.request('control.acquire', vm.vm_id, { instance_id: vm.status.instance_id }) as ControlLease
      if (attempt !== acquisition.current || !document.hasFocus()) {
        await window.palette.request('control.release', vm.vm_id, { lease_id: result.lease_id })
        return
      }
      lease.current = result; geometry.current.epoch = result.display_epoch
      queue.current = new InputQueue(async (seq, events) => {
        if (!lease.current) return
        const { width, height, epoch } = geometry.current
        return window.palette.request('control.input', vm.vm_id, { lease_id: lease.current.lease_id, seq, display_epoch: epoch, width, height, events })
      }, error => { reportInputFailure(error); void release() }, timing => {
        window.dispatchEvent(new CustomEvent('palette-input-timing', { detail: timing }))
      })
      setControlling(true); surface.current?.focus()
    } catch (error) { reportError(String(error)) }
    finally { if (attempt === acquisition.current) setAcquiring(false) }
  }

  function send(event: InputEvent): void { queue.current?.push(event) }
  function point(event: React.PointerEvent, captured = false): { x: number; y: number } | null {
    const canvas = mount.current?.querySelector('canvas')
    if (!canvas) return null
    return pointerPosition(canvas.getBoundingClientRect(), geometry.current.width, geometry.current.height, event.clientX, event.clientY, captured)
  }
  function key(event: React.KeyboardEvent, down: boolean): void {
    if (!lease.current || event.target !== surface.current) return
    const modifier = ['ControlLeft', 'ControlRight', 'AltLeft', 'AltRight'].includes(event.code)
    if (down && event.ctrlKey && event.altKey && modifier && !event.getModifierState('AltGraph')) modifierOnly.current = true
    if (down && !modifier) modifierOnly.current = false
    if (!down && modifier && modifierOnly.current) { event.preventDefault(); event.stopPropagation(); modifierOnly.current = false; void release(); return }
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
  function changeScale(value: string): void {
    selectedScale.current = value
    setScale(value)
    if (rfb.current) rfb.current.scaleViewport = value === 'fit'
  }

  return <section ref={panel} className="console-panel" onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) void release() }}>
    <div className="console-toolbar">
      <div className="flex items-center gap-2"><span className={`status-dot ${connected ? 'online' : ''}`} /><span>{connected ? controlling ? '人工控制' : '仅查看' : '未连接'}</span>{onAction && <ButtonGroup aria-label="虚拟机电源操作"><IconButton label="关机" title="强制停止虚拟机，需确认" disabled={busy || !activeVm(vm)} onClick={() => onAction('power')}><Power size={15} /></IconButton><IconButton label="重启" title="强制重启虚拟机" disabled={busy || !running || !vm.persistent} onClick={() => onAction('restart')}><RotateCw size={15} /></IconButton></ButtonGroup>}</div>
      <div className="flex items-center gap-2">
        <label className="flex items-center gap-1 text-xs" title={guestStatus?.desktop.resize ? '接管时自动调整客户机分辨率' : '需要 virtio-gpu 和客户机会话工具'}><input type="checkbox" checked={autoResize} disabled={!guestStatus?.desktop.resize} onChange={event => { setAutoResize(event.target.checked); autoResizeRef.current = event.target.checked }} />自动分辨率</label>
        <Button variant={controlling ? 'default' : 'secondary'} disabled={!running || !connected || acquiring} onClick={() => controlling ? void release() : void acquire()}><PcMouseIcon className="size-4" />{acquiring ? '正在接管…' : controlling ? '结束控制' : '接管'}</Button>
        <Select value={scale} items={{ fit: '适应窗口', native: '原始尺寸' }} onValueChange={value => { if (value) changeScale(value) }} onOpenChange={open => { scaleMenuOpen.current = open }}>
          <SelectTrigger aria-label="画面缩放" className="console-scale-select"><SelectValue /></SelectTrigger>
          <SelectContent container={panel} align="end">
            <SelectItem value="fit">适应窗口</SelectItem>
            <SelectItem value="native">原始尺寸</SelectItem>
          </SelectContent>
        </Select>
        <DropdownMenu><DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" className="console-overflow" aria-label="更多画面操作" />}><MoreHorizontal size={16} /></DropdownMenuTrigger><DropdownMenuContent align="end">
          <DropdownMenuItem onClick={() => changeScale('fit')}>适应窗口{scale === 'fit' ? '（当前）' : ''}</DropdownMenuItem>
          <DropdownMenuItem onClick={() => changeScale('native')}>原始尺寸{scale === 'native' ? '（当前）' : ''}</DropdownMenuItem>
          {onAction && <><DropdownMenuSeparator /><DropdownMenuItem title="强制重启虚拟机" disabled={busy || !running || !vm.persistent} onClick={() => onAction('restart')}>重启虚拟机</DropdownMenuItem></>}
        </DropdownMenuContent></DropdownMenu>
        <Button variant="ghost" size="icon" aria-label="重新连接画面" onClick={() => setRevision(value => value + 1)}><RefreshIcon className="size-4" /></Button>
        <Button variant="ghost" size="icon" aria-label={fullscreen ? '退出全屏' : '全屏'} title={fullscreen ? '退出全屏' : '全屏'} aria-pressed={fullscreen} disabled={fullscreenPending} onClick={() => void toggleFullscreen()}>{fullscreen ? <CollapseIcon className="size-4" /> : <ExpandIcon className="size-4" />}</Button>
      </div>
    </div>
    <div ref={surface} className={`console-surface ${controlling ? 'captured' : ''}`} role="region" tabIndex={0} aria-label={`${vm.name} 虚拟机画面，${controlling ? '人工控制中，按 Ctrl+Alt 释放' : '仅查看'}`} onKeyDown={event => key(event, true)} onKeyUp={event => key(event, false)}
      onContextMenu={event => event.preventDefault()}
      onPointerDownCapture={event => { if (!lease.current) return; const position = point(event); if (!position) return; event.preventDefault(); surface.current?.focus(); event.currentTarget.setPointerCapture(event.pointerId); send({ type: 'move', ...position }); const button = ['left', 'middle', 'right'][event.button]; if (button) send({ type: 'button', button: button as 'left' | 'middle' | 'right', down: true }) }}
      onPointerMoveCapture={event => { if (!lease.current) return; const position = point(event, event.currentTarget.hasPointerCapture(event.pointerId)); if (position) send({ type: 'move', ...position }) }}
      onPointerUpCapture={event => { if (!lease.current) return; const position = point(event, event.currentTarget.hasPointerCapture(event.pointerId)); if (position) send({ type: 'move', ...position }); const button = ['left', 'middle', 'right'][event.button]; if (button) send({ type: 'button', button: button as 'left' | 'middle' | 'right', down: false }); if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId) }}
      onPointerCancel={() => void release()}
      onWheelCapture={event => { if (!lease.current) return; for (const input of wheel.current.push(event.deltaX, event.deltaY, event.deltaMode, event.currentTarget.clientWidth, event.currentTarget.clientHeight)) send(input) }}>
      <div ref={mount} className="rfb-mount" />
      {controlling && <div className="human-control-label"><MousePointer2 size={14} />人工控制中<span>按 Ctrl+Alt 释放</span></div>}
      {!connected && !shuttingDown && <EmptyState variant="plain" heading="h2" className="console-placeholder" icon={vm.status.state === 'FAILED' ? <AlertTriangle size={24} /> : <Monitor size={24} />} title={displayError || ({ STOPPED: '虚拟机已关机', STARTING: '正在启动虚拟机…', PAUSED: '虚拟机已暂停', FAILED: '虚拟机发生故障' }[vm.status.state] ?? '正在连接画面…')} description={vm.status.last_error && `${vm.status.last_error.code}: ${vm.status.last_error.message}`} action={<>{(displayError || vm.status.last_error) && <CopyDebugInfoButton source="application" error={[displayError, vm.status.last_error && `${vm.status.last_error.code}: ${vm.status.last_error.message}`].filter(Boolean).join("; ")} vmId={vm.vm_id} />}{onAction && ['STOPPED', 'FAILED'].includes(vm.status.state) && <Button size="sm" disabled={busy} onClick={() => onAction('start')}><Play size={16} />启动</Button>}{running && displayError && <Button variant="secondary" size="sm" onClick={() => setRevision(value => value + 1)}>重新连接</Button>}</>} />}
    </div>
    <div className="console-footer"><span>{geometry.current.width ? `${geometry.current.width} × ${geometry.current.height}` : '—'}</span>{controlling ? <div className="flex items-center gap-2"><Button variant="ghost" size="sm" onClick={() => chord(['ctrl', 'alt', 'delete'])}>Ctrl + Alt + Del</Button><Button variant="ghost" size="sm" onClick={() => chord(['meta_l'])}>Super</Button></div> : <span>画面仅用于查看，接管后可操作</span>}</div>
  </section>
}
