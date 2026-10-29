import type { InputEvent } from '../../shared/types'

export function pointerPosition(rect: Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>, width: number, height: number, clientX: number, clientY: number, captured = false): { x: number; y: number } | null {
  if (!rect.width || !rect.height || width <= 0 || height <= 0 || (!captured && (clientX < rect.left || clientY < rect.top || clientX >= rect.left + rect.width || clientY >= rect.top + rect.height))) return null
  return { x: Math.max(0, Math.min(width - 1, Math.floor((clientX - rect.left) * width / rect.width))), y: Math.max(0, Math.min(height - 1, Math.floor((clientY - rect.top) * height / rect.height))) }
}

export class WheelAccumulator {
  private x = 0
  private y = 0
  reset(): void { this.x = 0; this.y = 0 }
  push(deltaX: number, deltaY: number, mode: number, pageWidth: number, pageHeight: number): InputEvent[] {
    this.x += deltaX * (mode === 1 ? 16 : mode === 2 ? pageWidth : 1)
    this.y += deltaY * (mode === 1 ? 16 : mode === 2 ? pageHeight : 1)
    const events: InputEvent[] = []
    for (const axis of ['x', 'y'] as const) {
      let steps = Math.trunc(this[axis] / 100)
      this[axis] -= steps * 100
      const direction = axis === 'x' ? steps < 0 ? 'left' : 'right' : steps < 0 ? 'up' : 'down'
      while (steps) {
        const count = Math.min(8, Math.abs(steps))
        events.push({ type: 'wheel', direction, steps: count })
        steps -= Math.sign(steps) * count
      }
    }
    return events
  }
}

const inputErrorCodes = [
  'VM_NOT_RUNNING',
  'CONTROL_NOT_OWNED',
  'ACTION_OUTCOME_UNKNOWN',
  'INPUT_CHANNEL_CLOSED',
  'QMP_UNAVAILABLE',
  'STATE_CONFLICT'
] as const

/** The backend error code carried by an input failure, if it has one.
 *
 * The renderer only ever sees the message the main process produced, and Electron rewraps
 * it twice ("Error invoking remote method 'request': BackendError: <CODE>: <text>"), so
 * the code has to be read back out of the text to tell a guest that is not running from a
 * channel that is genuinely broken.
 */
export function inputErrorCode(error: unknown): string | undefined {
  const direct = (error as { code?: unknown } | null)?.code
  if (typeof direct === 'string' && direct) return direct
  const message = error instanceof Error ? error.message : String(error ?? '')
  return inputErrorCodes.find(code => new RegExp(`\\b${code}\\b`).test(message))
}

const keyCodes: Record<string, string> = {
  ControlLeft: 'ctrl', ControlRight: 'ctrl_r', ShiftLeft: 'shift', ShiftRight: 'shift_r', AltLeft: 'alt', AltRight: 'alt_r', MetaLeft: 'meta_l', MetaRight: 'meta_r', Enter: 'ret', Escape: 'esc', Space: 'spc', Tab: 'tab', Backspace: 'backspace', CapsLock: 'caps_lock', ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down', Delete: 'delete', Insert: 'insert', Home: 'home', End: 'end', PageUp: 'pgup', PageDown: 'pgdn', Minus: 'minus', Equal: 'equal', BracketLeft: 'bracket_left', BracketRight: 'bracket_right', Backslash: 'backslash', Semicolon: 'semicolon', Quote: 'apostrophe', Backquote: 'grave_accent', Comma: 'comma', Period: 'dot', Slash: 'slash', NumLock: 'num_lock', NumpadEnter: 'kp_enter', NumpadAdd: 'kp_add', NumpadSubtract: 'kp_subtract', NumpadMultiply: 'kp_multiply', NumpadDivide: 'kp_divide', NumpadDecimal: 'kp_decimal', PrintScreen: 'print', Pause: 'pause', ScrollLock: 'scroll_lock'
}
export function physicalKey(code: string): string | undefined {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase()
  if (/^Digit[0-9]$/.test(code)) return code.slice(5)
  if (/^Numpad[0-9]$/.test(code)) return 'kp_' + code.slice(6)
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) return code.toLowerCase()
  return keyCodes[code]
}

export class InputQueue {
  private pending: InputEvent[] = []
  private busy = false
  private stopped = false
  private seq = 0
  private timer: ReturnType<typeof setTimeout> | undefined
  private lastSent = -Infinity
  private queuedAt = 0
  private scheduled = false
  private samples: { queue_ms: number; roundtrip_ms: number }[] = []
  constructor(private send: (seq: number, events: InputEvent[]) => Promise<unknown>, private fail: (error: unknown) => void, private sample?: (timing: { queue_ms: number; roundtrip_ms: number }) => void) {}
  push(event: InputEvent): void {
    if (this.stopped) return
    if (!this.pending.length) this.queuedAt = performance.now()
    if (event.type === 'move' && this.pending.at(-1)?.type === 'move') this.pending[this.pending.length - 1] = event
    else this.pending.push(event)
    if (this.pending.length > 512) { this.stop(); this.fail(new Error('输入队列已满，控制已释放')); return }
    this.schedule()
  }
  stop(): void { this.stopped = true; this.pending = []; clearTimeout(this.timer); this.timer = undefined }
  metrics(): ReadonlyArray<{ queue_ms: number; roundtrip_ms: number }> { return this.samples.slice() }
  private schedule(): void {
    if (this.stopped || this.busy || this.scheduled || !this.pending.length) return
    clearTimeout(this.timer)
    const urgent = this.pending.some(event => event.type !== 'move')
    const delay = urgent ? 0 : Math.max(0, 8 - (performance.now() - this.lastSent))
    // A microtask batches one DOM dispatch (position + button) without a timer wait.
    if (!delay) {
      this.scheduled = true
      queueMicrotask(() => { this.scheduled = false; void this.flush() })
    }
    else this.timer = setTimeout(() => { this.timer = undefined; void this.flush() }, delay)
  }
  async flush(): Promise<void> {
    if (this.stopped || this.busy || !this.pending.length) return
    clearTimeout(this.timer); this.timer = undefined
    this.busy = true
    const events = this.pending.splice(0, 64)
    const started = performance.now()
    const queueMs = started - this.queuedAt
    this.lastSent = started
    try { await this.send(++this.seq, events) }
    catch (error) { if (!this.stopped) { this.stop(); this.fail(error) } }
    finally {
      const timing = { queue_ms: queueMs, roundtrip_ms: performance.now() - started }
      this.samples.push(timing)
      if (this.samples.length > 256) this.samples.shift()
      this.busy = false
      this.schedule()
      this.sample?.(timing)
    }
  }
}
