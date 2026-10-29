import { afterEach, describe, it, expect, vi } from 'vitest'
import { InputQueue, WheelAccumulator, inputErrorCode, physicalKey, pointerPosition } from '../src/renderer/lib/input'
import type { InputEvent } from '../src/shared/types'

afterEach(() => vi.useRealTimers())

describe('input failure classification', () => {
  it('reads the backend code out of an Electron-wrapped rejection', () => {
    expect(inputErrorCode(new Error("Error invoking remote method 'request': BackendError: VM_NOT_RUNNING: 虚拟机未在运行"))).toBe('VM_NOT_RUNNING')
    expect(inputErrorCode(new Error('CONTROL_NOT_OWNED: 请先接管虚拟机'))).toBe('CONTROL_NOT_OWNED')
  })
  it('prefers a structured code and leaves unrelated failures unclassified', () => {
    expect(inputErrorCode({ code: 'ACTION_OUTCOME_UNKNOWN', message: 'x' })).toBe('ACTION_OUTCOME_UNKNOWN')
    expect(inputErrorCode(new Error('输入队列已满，控制已释放'))).toBeUndefined()
    expect(inputErrorCode(undefined)).toBeUndefined()
  })
})

describe('console coordinates and input sequencing', () => {
  it('maps CSS coordinates to framebuffer pixels independently of DPR', () => {
    expect(pointerPosition({ left: 100, top: 50, width: 640, height: 400 }, 1280, 800, 420, 250)).toEqual({ x: 640, y: 400 })
    expect(pointerPosition({ left: 100, top: 50, width: 640, height: 400 }, 1280, 800, 90, 50)).toBeNull()
    expect(physicalKey('KeyA')).toBe('a')
    expect(physicalKey('ControlRight')).toBe('ctrl_r')
    expect(physicalKey('MediaPlayPause')).toBeUndefined()
  })
  it('coalesces only adjacent moves and preserves button releases', async () => {
    const batches: InputEvent[][] = []
    const queue = new InputQueue(async (_seq, events) => { batches.push(events) }, error => { throw error })
    queue.push({ type: 'move', x: 1, y: 1 }); queue.push({ type: 'move', x: 2, y: 2 })
    queue.push({ type: 'button', button: 'left', down: true })
    queue.push({ type: 'move', x: 3, y: 3 }); queue.push({ type: 'button', button: 'left', down: false })
    await queue.flush()
    expect(batches[0]).toHaveLength(4)
    expect(batches[0][0]).toEqual({ type: 'move', x: 2, y: 2 })
    expect(batches[0][3]).toEqual({ type: 'button', button: 'left', down: false })
  })
  it('never replays input after an uncertain transport failure', async () => {
    let sent = 0, failed = 0
    const queue = new InputQueue(async () => { sent++; throw new Error('disconnected') }, () => { failed++ })
    queue.push({ type: 'key', key: 'a', down: true }); await queue.flush()
    queue.push({ type: 'key', key: 'a', down: true }); await queue.flush()
    expect(sent).toBe(1); expect(failed).toBe(1)
  })
  it('submits discrete input without advancing timers and drains immediately after ACK', async () => {
    vi.useFakeTimers()
    const batches: InputEvent[][] = []
    let acknowledge!: () => void
    const queue = new InputQueue(async (_seq, events) => {
      batches.push(events)
      if (batches.length === 1) await new Promise<void>(resolve => { acknowledge = resolve })
    }, error => { throw error })
    queue.push({ type: 'key', key: 'a', down: true })
    await vi.advanceTimersByTimeAsync(0)
    expect(batches).toHaveLength(1)
    queue.push({ type: 'move', x: 1, y: 1 })
    queue.push({ type: 'move', x: 2, y: 2 })
    queue.push({ type: 'key', key: 'a', down: false })
    acknowledge()
    await vi.advanceTimersByTimeAsync(0)
    expect(batches[1]).toEqual([{ type: 'move', x: 2, y: 2 }, { type: 'key', key: 'a', down: false }])
    queue.stop()
  })
  it('limits motion to an 8ms interval while urgent input bypasses the interval', async () => {
    vi.useFakeTimers()
    const send = vi.fn(async () => {})
    const queue = new InputQueue(send, error => { throw error })
    queue.push({ type: 'move', x: 1, y: 1 })
    await vi.advanceTimersByTimeAsync(0)
    queue.push({ type: 'move', x: 2, y: 2 })
    await vi.advanceTimersByTimeAsync(7)
    expect(send).toHaveBeenCalledTimes(1)
    queue.push({ type: 'button', button: 'left', down: true })
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(2)
    queue.push({ type: 'move', x: 3, y: 3 })
    await vi.advanceTimersByTimeAsync(8)
    expect(send).toHaveBeenCalledTimes(3)
    queue.stop()
  })
  it('does not send buffered input after stop and a late ACK', async () => {
    let acknowledge!: () => void
    const send = vi.fn(() => new Promise<void>(resolve => { acknowledge = resolve }))
    const queue = new InputQueue(send, () => {})
    queue.push({ type: 'key', key: 'a', down: true })
    const flush = queue.flush()
    queue.push({ type: 'key', key: 'a', down: false })
    queue.stop(); acknowledge(); await flush
    await Promise.resolve()
    expect(send).toHaveBeenCalledTimes(1)
  })
  it('ignores a late transport rejection after release instead of releasing a new queue', async () => {
    let reject!: (error: Error) => void
    const fail = vi.fn()
    const queue = new InputQueue(() => new Promise((_resolve, failed) => { reject = failed }), fail)
    queue.push({ type: 'key', key: 'a', down: true })
    const pending = queue.flush()
    queue.stop()
    reject(new Error('old stream closed'))
    await pending
    expect(fail).not.toHaveBeenCalled()
  })
  it('keeps batches bounded and releases on pending queue overflow', async () => {
    const send = vi.fn(async (_seq: number, events: InputEvent[]) => { expect(events.length).toBeLessThanOrEqual(64) })
    const fail = vi.fn()
    const queue = new InputQueue(send, fail)
    for (let i = 0; i < 513; i++) queue.push({ type: 'key', key: 'a', down: i % 2 === 0 })
    await queue.flush()
    expect(fail).toHaveBeenCalledTimes(1)
    expect(send).not.toHaveBeenCalled()
  })
  it('clamps captured dragging at all framebuffer edges', () => {
    const rect = { left: 100, top: 50, width: 640, height: 400 }
    expect(pointerPosition(rect, 1280, 800, 20, 900, true)).toEqual({ x: 0, y: 799 })
    expect(pointerPosition(rect, 1280, 800, 900, 0, true)).toEqual({ x: 1279, y: 0 })
    expect(pointerPosition(rect, 0, 0, 120, 60, true)).toBeNull()
  })
  it('accumulates tiny wheel deltas, normalizes modes and retains both axes', () => {
    const wheel = new WheelAccumulator()
    for (let i = 0; i < 99; i++) expect(wheel.push(0, 1, 0, 640, 400)).toEqual([])
    expect(wheel.push(0, 1, 0, 640, 400)).toEqual([{ type: 'wheel', direction: 'down', steps: 1 }])
    expect(wheel.push(0, -3, 1, 640, 400)).toEqual([])
    expect(wheel.push(0, -4, 1, 640, 400)).toEqual([{ type: 'wheel', direction: 'up', steps: 1 }])
    wheel.reset()
    expect(wheel.push(1, -1, 2, 1000, 400)).toEqual([
      { type: 'wheel', direction: 'right', steps: 8 }, { type: 'wheel', direction: 'right', steps: 2 }, { type: 'wheel', direction: 'up', steps: 4 }
    ])
    wheel.push(0, 70, 0, 640, 400)
    expect(wheel.push(0, -70, 0, 640, 400)).toEqual([])
    wheel.reset()
    expect(wheel.push(0, 30, 0, 640, 400)).toEqual([])
  })
})
