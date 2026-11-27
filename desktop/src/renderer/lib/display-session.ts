import RFB from '@novnc/novnc'
import { decodeRawFrame, type RawDisplayTicket } from './raw-display'

export interface DisplaySession extends EventTarget {
  scaleViewport: boolean
  readonly canvas: HTMLCanvasElement | null
  disconnect(): void
}

class VncSession extends EventTarget implements DisplaySession {
  private client: RFB
  private observer: MutationObserver
  private cachedCanvas: HTMLCanvasElement | null = null
  constructor(private target: HTMLElement, ticket: RawDisplayTicket, fit: boolean) {
    super()
    this.client = new RFB(target, ticket.url, { shared: true })
    this.client.viewOnly = true; this.client.scaleViewport = fit
    this.client.resizeSession = false; this.client.focusOnClick = false
    for (const name of ['connect', 'disconnect']) this.client.addEventListener(name, () => {
      if (this.canvas) this.canvas.dataset.paletteDisplay = 'rfb'
      this.dispatchEvent(new Event(name))
    })
    this.observer = new MutationObserver(() => {
      this.cachedCanvas = null
      this.dispatchEvent(new Event('geometry'))
    })
    this.observer.observe(target, { childList: true, subtree: true, attributes: true, attributeFilter: ['width', 'height'] })
  }
  get canvas() {
    if (this.cachedCanvas?.isConnected) return this.cachedCanvas
    this.cachedCanvas = this.target.querySelector('canvas')
    return this.cachedCanvas
  }
  get scaleViewport() { return this.client.scaleViewport }
  set scaleViewport(value: boolean) { this.client.scaleViewport = value }
  disconnect() { this.observer.disconnect(); this.cachedCanvas = null; this.client.disconnect() }
}

class RawSession extends EventTarget implements DisplaySession {
  readonly canvas = document.createElement('canvas')
  private ctx: CanvasRenderingContext2D | null
  private socket: WebSocket
  private observer: ResizeObserver
  private stage = document.createElement('div')
  private fit: boolean
  private seq = 0
  private connected = false
  private lastFrameAt = 0
  private cursor = ''
  private cursorStyle = 'default'
  private appliedCursor = ''
  constructor(target: HTMLElement, ticket: RawDisplayTicket, fit: boolean) {
    super()
    this.fit = fit
    this.ctx = this.canvas.getContext('2d', { alpha: false, desynchronized: true }) ?? this.canvas.getContext('2d')
    Object.assign(this.stage.style, { width: '100%', height: '100%', display: 'flex', overflow: 'auto' })
    Object.assign(this.canvas.style, { margin: 'auto', flexShrink: '0' })
    this.canvas.width = 0; this.canvas.height = 0
    this.canvas.dataset.paletteDisplay = 'raw'
    this.stage.append(this.canvas); target.append(this.stage)
    this.observer = new ResizeObserver(() => this.resize())
    this.observer.observe(this.stage)
    this.socket = new WebSocket(ticket.url)
    this.socket.binaryType = 'arraybuffer'
    this.socket.onmessage = event => {
      try {
        if (!(event.data instanceof ArrayBuffer)) throw new Error('画面数据无效')
        const frame = decodeRawFrame(event.data, ticket)
        if (frame.seq <= this.seq) throw new Error('画面序号倒退')
        this.seq = frame.seq
        const changed = this.canvas.width !== frame.width || this.canvas.height !== frame.height
        if (changed) { this.canvas.width = frame.width; this.canvas.height = frame.height; this.resize() }
        if (!this.ctx) throw new Error('无法创建画布')
        if (frame.kind === 'full') {
          this.ctx.putImageData(new ImageData(frame.pixels, frame.width, frame.height), 0, 0)
        } else if (frame.kind === 'rect') {
          if (changed || !this.connected || !frame.rect) throw new Error('增量画面缺少基准帧')
          const [x, y, rw, rh] = frame.rect
          this.ctx.putImageData(new ImageData(frame.pixels, rw, rh), x, y)
        } else if (!this.connected || changed) {
          throw new Error('光标更新缺少基准帧')
        }
        if (frame.cursor?.rgba && frame.cursor.rgba !== this.cursor) {
          this.cursor = frame.cursor.rgba
          const bytes = Uint8ClampedArray.from(atob(frame.cursor.rgba), char => char.charCodeAt(0))
          const shape = document.createElement('canvas')
          shape.width = frame.cursor.width!; shape.height = frame.cursor.height!
          shape.getContext('2d')?.putImageData(new ImageData(bytes, shape.width, shape.height), 0, 0)
          this.cursorStyle = `url(${shape.toDataURL()}) ${frame.cursor.hot_x} ${frame.cursor.hot_y}, default`
        }
        const nextCursor = frame.cursor?.visible === false ? 'none' : this.cursorStyle
        if (nextCursor !== this.appliedCursor) {
          this.appliedCursor = nextCursor
          this.canvas.style.cursor = nextCursor
        }
        if (changed) this.dispatchEvent(new Event('geometry'))
        if (!this.connected) { this.connected = true; this.dispatchEvent(new Event('connect')) }
        const now = performance.now()
        window.dispatchEvent(new CustomEvent('palette-display-timing', { detail: { provider: 'raw', seq: frame.seq, generation: frame.generation, frame_interval_ms: this.lastFrameAt ? now - this.lastFrameAt : null } }))
        this.lastFrameAt = now
        if (this.socket.readyState === WebSocket.OPEN) {
          const ack = new ArrayBuffer(8)
          new DataView(ack).setBigUint64(0, BigInt(frame.seq))
          this.socket.send(ack)
        }
      } catch { this.socket.close(1002, 'invalid raw display frame') }
    }
    this.socket.onclose = () => this.dispatchEvent(new Event('disconnect'))
    this.socket.onerror = () => this.socket.close()
  }
  get scaleViewport() { return this.fit }
  set scaleViewport(value: boolean) { this.fit = value; this.resize() }
  private resize() {
    const ratio = this.fit && this.canvas.width && this.canvas.height ? Math.min(this.stage.clientWidth / this.canvas.width, this.stage.clientHeight / this.canvas.height) : 1
    this.canvas.style.width = `${this.canvas.width * ratio}px`
    this.canvas.style.height = `${this.canvas.height * ratio}px`
  }
  disconnect() { this.observer.disconnect(); this.socket.close(); this.stage.remove() }
}

export function createDisplaySession(target: HTMLElement, ticket: RawDisplayTicket, fit: boolean): DisplaySession {
  return ticket.provider === 'raw' ? new RawSession(target, ticket, fit) : new VncSession(target, ticket, fit)
}
