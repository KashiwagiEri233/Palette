import { expect, it, vi } from 'vitest'
import { createServer, Socket } from 'node:net'
import { randomBytes } from 'node:crypto'
import { mkdtemp, mkdir, writeFile, rm, unlink } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { BackendError, InputStream } from '../src/main/ipc'

interface Request { version: number; generation: string; request_id: string; command: { method: string; payload: Record<string, unknown> } }

async function fixture(run: (context: { root: string; received: Request[]; connections: () => number; respond: (request: Request) => Record<string, unknown> | null; disconnect: () => void }) => Promise<void>, mode: 'normal' | 'disconnect' | 'generation' = 'normal') {
  const root = await mkdtemp(join(tmpdir(), 'palette-stream-'))
  const directory = join(root, '.operator')
  await mkdir(directory, { mode: 0o700 })
  const address = process.platform === 'win32' ? '\\\\.\\pipe\\palette-stream-' + randomBytes(8).toString('hex') : join(directory, 'test.sock')
  const generation = 'a'.repeat(32)
  await writeFile(join(directory, 'endpoint.json'), JSON.stringify({ address, generation, token: 'b'.repeat(64), operation_timeout_seconds: 1 }), { mode: 0o600 })
  const received: Request[] = []
  const sockets = new Set<Socket>()
  let count = 0
  const respond = (request: Request): Record<string, unknown> | null => {
    if (request.command.method === 'control.stream.open') return { stream: 'operator-input-stream-v1' }
    return { seq: request.command.payload.seq }
  }
  const server = createServer(socket => {
    count++; sockets.add(socket)
    socket.on('error', () => {})
    socket.on('close', () => sockets.delete(socket))
    let buffer = Buffer.alloc(0)
    socket.on('data', data => {
      buffer = Buffer.concat([buffer, data])
      while (buffer.length >= 4 && buffer.length >= buffer.readUInt32BE(0) + 4) {
        const size = buffer.readUInt32BE(0)
        const request = JSON.parse(buffer.subarray(4, size + 4).toString()) as Request
        buffer = buffer.subarray(size + 4); received.push(request)
        if (mode === 'disconnect' && request.command.method === 'control.input') { socket.destroy(); return }
        const body = Buffer.from(JSON.stringify({ version: 1, generation: mode === 'generation' && request.command.method === 'control.input' ? 'c'.repeat(32) : generation, request_id: request.request_id, ok: true, result: respond(request) }))
        const header = Buffer.alloc(4); header.writeUInt32BE(body.length)
        // Force header and body fragmentation over the real local transport.
        socket.write(header.subarray(0, 1)); socket.write(header.subarray(1)); socket.write(body)
      }
    })
  })
  await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(address, resolve) })
  try { await run({ root, received, connections: () => count, respond, disconnect: () => { for (const socket of sockets) socket.end() } }) }
  finally {
    for (const socket of sockets) socket.destroy()
    await new Promise<void>(resolve => server.close(() => resolve()))
    await rm(root, { recursive: true, force: true })
  }
}

it('reuses one authenticated connection without rereading endpoint files per batch', async () => {
  await fixture(async ({ root, received, connections }) => {
    const stream = await InputStream.open(root, 'desktop', 'lease', 'a'.repeat(32))
    try {
      await unlink(join(root, '.operator', 'endpoint.json'))
      for (let seq = 1; seq <= 10; seq++) expect(await stream.input({ lease_id: 'lease', seq })).toEqual({ seq })
      expect(connections()).toBe(1)
      expect(received).toHaveLength(11)
      await expect(stream.input({ lease_id: 'other', seq: 11 })).rejects.toThrow('租约已更换')
    } finally { stream.close() }
  })
})

it('retires an idle stream as soon as the peer ends it and notifies late observers', async () => {
  await fixture(async ({ root, received, disconnect }) => {
    const stream = await InputStream.open(root, 'desktop', 'lease', 'a'.repeat(32))
    const failed = vi.fn()
    stream.setFailureHandler(failed)
    disconnect()
    await vi.waitFor(() => expect(failed).toHaveBeenCalledTimes(1))
    expect(failed.mock.calls[0][0]).toMatchObject({ code: 'INPUT_CHANNEL_CLOSED', details: { effect_state: 'not_submitted', phase: 'peer_end' } })
    await expect(stream.input({ lease_id: 'lease', seq: 1 })).rejects.toThrow('重新接管')
    expect(received).toHaveLength(1)
    const late = vi.fn()
    stream.setFailureHandler(late)
    expect(late).toHaveBeenCalledWith(failed.mock.calls[0][0])
    stream.close()
    expect(failed).toHaveBeenCalledTimes(1)
  })
})

it.each(['callback', 'throw'] as const)('preserves EPIPE evidence, fails once and never replays after a write %s failure', async mode => {
  await fixture(async ({ root, received, connections }) => {
    const stream = await InputStream.open(root, 'desktop', 'lease', 'a'.repeat(32))
    const failed = vi.fn()
    stream.setFailureHandler(failed)
    const original = Socket.prototype.write
    const broken = vi.spyOn(Socket.prototype, 'write').mockImplementation(function (this: Socket, chunk: Uint8Array | string, ...args: unknown[]): boolean {
      if (Buffer.isBuffer(chunk) && chunk.includes('"method":"control.input"')) {
        const error = Object.assign(new Error('write EPIPE'), { code: 'EPIPE' })
        if (mode === 'throw') throw error
        const callback = args.find(item => typeof item === 'function') as (error: Error) => void
        queueMicrotask(() => callback(error))
        return false
      }
      return Reflect.apply(original, this, [chunk, ...args]) as boolean
    })
    try {
      const error = await stream.input({ lease_id: 'lease', seq: 1 }).catch(error => error)
      expect(error).toBeInstanceOf(BackendError)
      expect(error).toMatchObject({ code: 'ACTION_OUTCOME_UNKNOWN', details: { phase: 'write', effect_state: 'unknown', transport_error: { code: 'EPIPE', message: 'write EPIPE', stack: expect.stringContaining('EPIPE') } } })
      expect(error.message).toContain('检查画面后重新接管')
      await expect(stream.input({ lease_id: 'lease', seq: 1 })).rejects.toBe(error)
      expect(failed).toHaveBeenCalledTimes(1)
      expect(received).toHaveLength(1)
      expect(connections()).toBe(1)
    } finally { broken.mockRestore(); stream.close() }
  })
})

it.each(['disconnect', 'generation'] as const)('never retries an input after %s', async mode => {
  await fixture(async ({ root, received, connections }) => {
    const stream = await InputStream.open(root, 'desktop', 'lease', 'a'.repeat(32))
    try {
      await expect(stream.input({ lease_id: 'lease', seq: 1 })).rejects.toThrow()
      await expect(stream.input({ lease_id: 'lease', seq: 1 })).rejects.toThrow()
      expect(received.filter(request => request.command.method === 'control.input')).toHaveLength(1)
      expect(connections()).toBe(1)
    } finally { stream.close() }
  }, mode)
})
