import type { InputEvent } from '../../shared/types'

export function pointerPosition(rect: Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>, width: number, height: number, clientX: number, clientY: number): { x: number; y: number } | null {
  if (!rect.width || !rect.height || clientX < rect.left || clientY < rect.top || clientX >= rect.left + rect.width || clientY >= rect.top + rect.height) return null
  return { x: Math.min(width - 1, Math.floor((clientX - rect.left) * width / rect.width)), y: Math.min(height - 1, Math.floor((clientY - rect.top) * height / rect.height)) }
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
  constructor(private send: (seq: number, events: InputEvent[]) => Promise<unknown>, private fail: (error: unknown) => void) {}
  push(event: InputEvent): void {
    if (this.stopped) return
    if (event.type === 'move' && this.pending.at(-1)?.type === 'move') this.pending[this.pending.length - 1] = event
    else this.pending.push(event)
    if (this.pending.length > 512) { this.stop(); this.fail(new Error('输入队列已满，控制已释放')) }
  }
  stop(): void { this.stopped = true; this.pending = [] }
  async flush(): Promise<void> {
    if (this.stopped || this.busy || !this.pending.length) return
    this.busy = true
    const events = this.pending.splice(0, 64)
    try { await this.send(++this.seq, events) }
    catch (error) { this.stop(); this.fail(error) }
    finally { this.busy = false }
  }
}
