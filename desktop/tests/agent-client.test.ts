import { it, expect } from 'vitest'
import { createServer, type Socket } from 'node:net'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { AgentClient } from '../src/main/agent-client'

async function fixture(reply: (socket: Socket, request: Record<string, unknown>) => void) {
  const directory = await mkdtemp(join(tmpdir(), 'palette-agent-ipc-'))
  const address = process.platform === 'win32' ? `\\\\.\\pipe\\palette-agent-${Date.now()}-${Math.random()}` : join(directory, 'a.sock')
  const sockets = new Set<Socket>()
  const server = createServer(socket => {
    sockets.add(socket)
    socket.on('close', () => sockets.delete(socket))
    let bytes = Buffer.alloc(0)
    socket.on('data', data => {
      bytes = Buffer.concat([bytes, data])
      if (bytes.length >= 4 && bytes.length >= bytes.readUInt32BE(0) + 4) reply(socket, JSON.parse(bytes.subarray(4).toString('utf8')))
    })
  })
  await new Promise<void>(resolve => server.listen(address, resolve))
  const client = new AgentClient({ address, generation: 'a'.repeat(32), token: 'b'.repeat(64), operation_timeout_seconds: 1 })
  return { client, close: async () => { for (const socket of sockets) socket.destroy(); await new Promise<void>(resolve => server.close(() => resolve())); await rm(directory, { recursive: true, force: true }) } }
}
function frame(value: unknown): Buffer { const bytes = Buffer.from(JSON.stringify(value)); const header = Buffer.alloc(4); header.writeUInt32BE(bytes.length); return Buffer.concat([header, bytes]) }

it('decodes fragmented JSON and the binary image without losing identity or claim', async () => {
  const test = await fixture((socket, request) => {
    expect(request.claim_token).toBe('c'.repeat(48))
    expect(request.command).toEqual({ method: 'observe_image', parameters: { vm_id: 'vm' } })
    const data = Buffer.concat([frame({ version: 1, request_id: request.request_id, generation: request.generation, ok: true, result: { frame_id: 'frame' }, image_bytes: 3 }), Buffer.from([0, 0, 0, 3, 1, 2, 3])])
    socket.write(data.subarray(0, 2))
    setTimeout(() => { socket.write(data.subarray(2, data.length - 2)); setTimeout(() => socket.end(data.subarray(data.length - 2)), 5) }, 5)
  })
  try { const result = await test.client.request('observe_image', 'vm', {}, 'c'.repeat(48)); expect(result.result.frame_id).toBe('frame'); expect([...result.image]).toEqual([1, 2, 3]) }
  finally { await test.close() }
})

it.each(['identity', 'length', 'disconnect'])('rejects %s failures without replaying the operation', async kind => {
  let calls = 0
  const test = await fixture((socket, request) => {
    calls++
    if (kind === 'disconnect') { socket.destroy(); return }
    socket.end(Buffer.concat([frame({ version: 1, request_id: kind === 'identity' ? 'wrong' : request.request_id, generation: request.generation, ok: true, result: {}, image_bytes: kind === 'length' ? 9 * 1024 * 1024 : 0 }), Buffer.alloc(4)]))
  })
  try { await expect(test.client.request('act', 'vm')).rejects.toThrow(); expect(calls).toBe(1) }
  finally { await test.close() }
})
