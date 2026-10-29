import { it, expect } from 'vitest'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { AgentCredentials } from '../src/main/agent-storage'
import { UnavailableGuestProgramming } from '../src/main/guest-programming'

it('persists only encrypted credentials and serializes concurrent modifications', async () => {
  const root = await mkdtemp(join(tmpdir(), 'palette-secret-test-'))
  const encryption = { available: () => true, encrypt: (text: string) => Buffer.from(text.split('').reverse().join('')), decrypt: (bytes: Buffer) => bytes.toString().split('').reverse().join('') }
  try {
    const store = new AgentCredentials(join(root, 'keys.json'), encryption)
    await Promise.all([store.modify('a', async () => ({ type: 'api_key', key: 'secret-a' })), store.modify('b', async () => ({ type: 'api_key', key: 'secret-b' }))])
    expect(await readFile(join(root, 'keys.json'), 'utf8')).not.toContain('secret-')
    expect(await store.list()).toHaveLength(2)
    expect(await new AgentCredentials(join(root, 'keys.json'), encryption).read('a')).toEqual({ type: 'api_key', key: 'secret-a' })
    expect(await store.redact('secret-a and secret-b')).toBe('[redacted] and [redacted]')
    const escapedKey = 'key-with-"quote\\slash'
    await store.modify('escaped', async () => ({ type: 'api_key', key: escapedKey }))
    const report = await store.redactJson({ error: escapedKey, nested: [escapedKey], modelId: 'fixture' })
    expect(report).toEqual({ error: '[redacted]', nested: ['[redacted]'], modelId: 'fixture' })
    expect(JSON.parse(JSON.stringify(report))).toEqual(report)
    expect(await store.redactJson({ type: 'image', mimeType: 'image/png', data: escapedKey, caption: escapedKey }))
      .toEqual({ type: 'image', mimeType: 'image/png', data: escapedKey, caption: '[redacted]' })
    await store.delete('a')
    expect(await store.read('a')).toBeUndefined()
  } finally { await rm(root, { recursive: true, force: true }) }
})
it('keeps secrets in memory when encryption is unavailable', async () => {
  const root = await mkdtemp(join(tmpdir(), 'palette-memory-key-'))
  try {
    const encryption = { available: () => false, encrypt: () => { throw new Error('must not encrypt') }, decrypt: () => { throw new Error('must not decrypt') } }
    const store = new AgentCredentials(join(root, 'keys.json'), encryption)
    await store.modify('a', async () => ({ type: 'api_key', key: 'ephemeral' }))
    expect(await store.read('a')).toEqual({ type: 'api_key', key: 'ephemeral' })
    expect(await new AgentCredentials(join(root, 'keys.json'), encryption).read('a')).toBeUndefined()
    await expect(readFile(join(root, 'keys.json'))).rejects.toMatchObject({ code: 'ENOENT' })
  } finally { await rm(root, { recursive: true, force: true }) }
})
it('guest programming placeholders fail explicitly', async () => {
  const adapter = new UnavailableGuestProgramming()
  expect(adapter.available).toBe(false)
  await expect(adapter.execute('echo test')).rejects.toThrow('GUEST_TOOL_UNAVAILABLE')
  await expect(adapter.read('/file')).rejects.toThrow('GUEST_TOOL_UNAVAILABLE')
  await expect(adapter.write('/file', 'test')).rejects.toThrow('GUEST_TOOL_UNAVAILABLE')
  await expect(adapter.edit('/file', 'a', 'b')).rejects.toThrow('GUEST_TOOL_UNAVAILABLE')
})
