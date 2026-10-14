import { it, expect } from 'vitest'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { call } from '../src/main/ipc'

it('refuses a replacement endpoint before sending an exit mutation', async () => {
  const root = await mkdtemp(join(tmpdir(), 'palette-ipc-generation-'))
  try {
    const directory = join(root, '.operator')
    await mkdir(directory, { mode: 0o700 })
    await writeFile(join(directory, 'endpoint.json'), JSON.stringify({
      generation: 'b'.repeat(32), token: 'a'.repeat(64), operation_timeout_seconds: 1,
      address: process.platform === 'win32' ? '\\\\.\\pipe\\palette-generation-test' : join(directory, 'generation.sock')
    }), { mode: 0o600 })
    await expect(call(root, 'runtime.stop', undefined, {}, 'a'.repeat(32))).rejects.toThrow('后台已更换')
  } finally {
    if (!resolve(root).startsWith(join(tmpdir(), 'palette-ipc-generation-'))) throw new Error('Unexpected cleanup root')
    await rm(root, { recursive: true, force: true })
  }
})
