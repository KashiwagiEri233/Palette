import { it, expect, vi } from 'vitest'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PiAgentService } from '../src/main/pi-agent'
import { AgentProviderStore, defaultConfiguration } from '../src/main/agent-providers'
import { atomicJson } from '../src/main/agent-storage'
import * as storage from '../src/main/agent-storage'
import type { AgentConfiguration, CustomAgentProvider } from '../src/shared/agent'
import type { Conversation } from '../src/shared/workspace'

const encryption = { available: () => true, encrypt: (text: string) => Buffer.from(text.split('').reverse().join('')), decrypt: (bytes: Buffer) => bytes.toString().split('').reverse().join('') }
const model = { id: 'same-id', name: 'Shared model', contextWindow: 128000, maxTokens: 8192, vision: false, reasoning: true }
const provider = (id = ''): CustomAgentProvider => ({ id, name: 'My service', api: 'openai-completions', baseUrl: 'http://127.0.0.1:1234/v1', models: [model] })

async function environment() {
  const root = await mkdtemp(join(tmpdir(), 'palette-providers-'))
  const conversations: Conversation[] = []
  const create = () => new PiAgentService(root, root, () => root, encryption, async () => conversations, () => {})
  const service = create()
  return { root, service, create, conversations, close: async () => { await service.close(); await rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 }) } }
}

it('persists independent providers and secrets, resolves trusted model metadata and preserves defaults', async () => {
  const test = await environment()
  try {
    const first = (await test.service.saveProvider({ ...provider(), name: 'First' }, 'first-secret')).providers.find(item => item.name === 'First')!
    const second = (await test.service.saveProvider({ ...provider(), name: 'Second' }, 'second-secret')).providers.find(item => item.name === 'Second')!
    expect(first.id).not.toBe(second.id)
    let settings = await test.service.settings()
    expect(settings.models.filter(item => item.id === model.id)).toHaveLength(2)
    const requested: AgentConfiguration = { ...defaultConfiguration, ...model, providerId: second.id, modelId: model.id, baseUrl: 'https://wrong.example', custom: false, api: 'openai-responses', vision: true, contextWindow: 2048, maxTokens: 1, maxSteps: 27, thinkingLevel: 'max' }
    settings = await test.service.updateSettings(requested)
    expect(settings.configuration).toMatchObject({ providerId: second.id, api: 'openai-completions', baseUrl: provider().baseUrl, contextWindow: 128000, maxTokens: 8192, custom: true, vision: false, maxSteps: 27, thinkingLevel: 'max' })
    expect(await test.service.credentials.read(first.id)).toEqual({ type: 'api_key', key: 'first-secret' })
    expect(await test.service.credentials.read(second.id)).toEqual({ type: 'api_key', key: 'second-secret' })
    await test.service.saveProvider({ ...provider(second.id), name: 'Renamed', models: [{ ...model, name: 'Edited', maxTokens: 4096 }] })
    expect((await test.service.settings()).configuration.maxTokens).toBe(4096)
    expect(await test.service.credentials.read(second.id)).toMatchObject({ key: 'second-secret' })
    const reopened = test.create()
    try {
      const restored = await reopened.settings()
      expect(restored.configuration).toMatchObject({ providerId: second.id, modelId: model.id, maxSteps: 27, thinkingLevel: 'max' })
      expect(restored.providers.find(item => item.id === second.id)).toMatchObject({ name: 'Renamed', credentialConfigured: true, modelCount: 1 })
      expect(JSON.stringify(restored)).not.toContain('secret')
      expect(await readFile(join(test.service.directory, 'providers.json'), 'utf8')).not.toContain('secret')
      expect(await readFile(join(test.service.directory, 'credentials.json'), 'utf8')).not.toContain('secret')
    } finally { await reopened.close() }
  } finally { await test.close() }
})

it('clears active selection when its key, model or provider is removed without switching to another service', async () => {
  const test = await environment()
  try {
    await test.service.saveProvider(provider('custom-first'), 'first-secret')
    await test.service.saveProvider(provider('custom-second'), 'second-secret')
    const select = () => test.service.updateSettings({ ...defaultConfiguration, providerId: 'custom-first', modelId: model.id })
    await select()
    await test.service.configureCredential('custom-first', null)
    expect((await test.service.settings()).configuration.modelId).toBe('')
    expect((await test.service.settings()).providers.find(item => item.id === 'custom-first')).toMatchObject({ credentialConfigured: false, managed: true })
    await expect(select()).rejects.toThrow('API Key')
    await test.service.configureCredential('custom-first', 'replacement')
    expect((await test.service.settings()).configuration.modelId).toBe('')
    await select()
    await test.service.saveProvider({ ...provider('custom-first'), models: [] })
    expect((await test.service.settings()).configuration.modelId).toBe('')
    await test.service.saveProvider(provider('custom-first'))
    await select()
    await test.service.deleteProvider('custom-first')
    expect((await test.service.settings()).configuration.modelId).toBe('')
    expect(await test.service.credentials.read('custom-first')).toBeUndefined()
    expect(await test.service.credentials.read('custom-second')).toMatchObject({ key: 'second-secret' })
  } finally { await test.close() }
})

it('migrates the legacy custom endpoint once and preserves the legacy file and credential identifier', async () => {
  const test = await environment()
  try {
    const legacy = { ...defaultConfiguration, ...model, api: 'openai-completions', custom: true, providerId: 'previous-unused-id', modelId: model.id, baseUrl: provider().baseUrl, thinkingLevel: 'high', maxSteps: 32 }
    await atomicJson(join(test.service.directory, 'settings.json'), legacy)
    await test.service.credentials.modify('palette-custom', async () => ({ type: 'api_key', key: 'legacy-secret' }))
    await test.service.credentials.modify('openai', async () => ({ type: 'api_key', key: 'builtin-secret' }))
    const migrated = await test.service.settings()
    expect(migrated.configuration).toMatchObject({ providerId: 'palette-custom', modelId: model.id, thinkingLevel: 'high', maxSteps: 32 })
    expect(migrated.providers.filter(item => item.custom)).toHaveLength(1)
    expect(migrated.providers.find(item => item.id === 'openai')).toMatchObject({ credentialConfigured: true })
    const reopened = test.create()
    try { expect((await reopened.settings()).providers.filter(item => item.custom)).toHaveLength(1) }
    finally { await reopened.close() }
    expect(JSON.parse(await readFile(join(test.service.directory, 'settings.json'), 'utf8'))).toEqual(legacy)
  } finally { await test.close() }
})

it('keeps the legacy state intact when migration cannot write its new file', async () => {
  const root = await mkdtemp(join(tmpdir(), 'palette-provider-migration-'))
  const legacy = { ...defaultConfiguration, modelId: 'legacy' }
  try {
    await atomicJson(join(root, 'settings.json'), legacy)
    const failure = vi.spyOn(storage, 'atomicJson').mockRejectedValueOnce(new Error('migration write failed'))
    const store = new AgentProviderStore(root)
    await expect(store.load()).rejects.toThrow('migration write failed')
    failure.mockRestore()
    expect(JSON.parse(await readFile(join(root, 'settings.json'), 'utf8'))).toEqual(legacy)
    expect((await store.load()).configuration.modelId).toBe('legacy')
  } finally { await rm(root, { recursive: true, force: true }) }
})

it('uses each custom provider key for real SDK requests and only discovers with a saved key at its saved endpoint', async () => {
  const test = await environment()
  const authorizations: (string | undefined)[] = []
  const server = createServer(async (request, response) => {
    authorizations.push(request.headers.authorization)
    if (request.method === 'GET') { response.end(JSON.stringify({ data: [{ id: model.id }] })); return }
    for await (const _chunk of request) { /* Consume the request before sending the SSE response. */ }
    response.writeHead(200, { 'content-type': 'text/event-stream' })
    response.end(`data: ${JSON.stringify({ id: 'test', object: 'chat.completion.chunk', created: 1, model: model.id, choices: [{ index: 0, delta: { role: 'assistant', content: 'Done' }, finish_reason: 'stop' }] })}\n\ndata: [DONE]\n\n`)
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const baseUrl = `http://127.0.0.1:${(server.address() as { port: number }).port}/v1`
  try {
    const id = randomUUID()
    test.conversations.push({ id, vm_id: 'vm', title: 'Providers', draft: '', pinned: false, archived: false, revision: 1, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() })
    for (const name of ['first', 'second']) {
      await test.service.saveProvider({ ...provider(`custom-${name}`), baseUrl }, `${name}-secret`)
      await test.service.updateSettings({ ...defaultConfiguration, providerId: `custom-${name}`, modelId: model.id })
      await test.service.start('vm', id, `Hello ${name}`, randomUUID())
      for (let i = 0; i < 200; i++) {
        const snapshot = await test.service.snapshot('vm', id)
        if (snapshot.view.run?.state === 'completed') break
        await new Promise(resolve => setTimeout(resolve, 20))
      }
      expect((await test.service.snapshot('vm', id)).view.run?.state).toBe('completed')
    }
    expect(authorizations).toEqual(['Bearer first-secret', 'Bearer second-secret'])
    const input = { api: 'openai-completions' as const, providerId: 'custom-first', baseUrl }
    expect((await test.service.discoverModels(input)).models).toHaveLength(1)
    expect(authorizations.at(-1)).toBe('Bearer first-secret')
    await expect(test.service.discoverModels({ ...input, baseUrl: `${baseUrl}/changed` })).rejects.toThrow('重新输入')
    await expect(test.service.discoverModels({ ...input, api: 'anthropic-messages' })).rejects.toThrow('重新输入')
    expect(authorizations).toHaveLength(3)
  } finally { await test.close(); server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())) }
}, 15000)

it('rolls back a changed key when provider metadata cannot be saved', async () => {
  const test = await environment()
  try {
    await test.service.saveProvider(provider('custom-first'), 'original-secret')
    const state = (test.service as unknown as { providerStore: AgentProviderStore }).providerStore
    const failure = vi.spyOn(state, 'save').mockRejectedValueOnce(new Error('disk failure'))
    await expect(test.service.saveProvider({ ...provider('custom-first'), name: 'Unsaved' }, 'new-secret')).rejects.toThrow('disk failure')
    failure.mockRestore()
    expect((await test.service.settings()).providers.find(item => item.id === 'custom-first')?.name).toBe('My service')
    expect(await test.service.credentials.read('custom-first')).toMatchObject({ key: 'original-secret' })
  } finally { await test.close() }
})

it('retains builtin entries without keys and validates model identifiers within each provider', async () => {
  const test = await environment()
  try {
    await test.service.configureCredential('openai', 'test-secret')
    await test.service.configureCredential('openai', null)
    expect((await test.service.settings()).providers.find(item => item.id === 'openai')).toMatchObject({ managed: true, credentialConfigured: false })
    await expect(test.service.saveProvider({ ...provider(), models: [model, model] })).rejects.toThrow('不能重复')
    await expect(test.service.deleteProvider('openai')).rejects.toThrow('自定义提供商不存在')
    await expect(test.service.configureCredential('unknown', 'test-secret')).rejects.toThrow('无效')
  } finally { await test.close() }
})

it('treats unavailable encrypted credentials as unconfigured after restarting without secure storage', async () => {
  const test = await environment()
  try {
    await test.service.saveProvider(provider('custom-first'), 'original-secret')
    await test.service.updateSettings({ ...defaultConfiguration, providerId: 'custom-first', modelId: model.id })
    const service = new PiAgentService(test.root, test.root, () => test.root, { ...encryption, available: () => false }, async () => [], () => {})
    try { expect((await service.settings()).credentialConfigured).toBe(false); expect((await service.settings()).configuration.modelId).toBe('') }
    finally { await service.close() }
  } finally { await test.close() }
})
