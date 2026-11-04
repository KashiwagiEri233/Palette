import { describe, expect, it } from 'vitest'
import { decodeRawFrame } from '../src/renderer/lib/raw-display'

const ticket = { url: 'ws://127.0.0.1/raw-display', provider: 'raw' as const, instance_id: 'inst_test', generation: 'generation' }
function packet(changes: Record<string, unknown> = {}, pixels = 8): ArrayBuffer {
  const header = new TextEncoder().encode(JSON.stringify({ version: 1, format: 'rgba', instance_id: ticket.instance_id, generation: ticket.generation, seq: 1, width: 2, height: 1, ...changes }))
  const buffer = new ArrayBuffer(4 + header.length + pixels)
  new DataView(buffer).setUint32(0, header.length)
  new Uint8Array(buffer, 4, header.length).set(header)
  return buffer
}
describe('raw framebuffer boundary', () => {
  it('decodes complete native pixels with ticket identity', () => {
    const frame = decodeRawFrame(packet(), ticket)
    expect(frame.width).toBe(2)
    expect(frame.pixels.byteLength).toBe(8)
  })
  it.each([{ instance_id: 'old' }, { generation: 'old' }, { seq: 0 }, { width: -1 }, { width: 8193 }, { format: 'jpeg' }, { width: 1.5 }, { version: 2 }])('rejects a stale or malformed header %j', changes => {
    expect(() => decodeRawFrame(packet(changes), ticket)).toThrow()
  })
  it('refuses truncated pixels and oversized metadata', () => {
    expect(() => decodeRawFrame(packet({}, 7), ticket)).toThrow()
    const data = packet()
    new DataView(data).setUint32(0, 0xffffffff)
    expect(() => decodeRawFrame(data, ticket)).toThrow()
  })
})
