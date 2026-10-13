import { describe, it, expect } from 'vitest'
import { InputQueue, physicalKey, pointerPosition } from '../src/renderer/lib/input'
import type { InputEvent } from '../src/shared/types'

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
})
