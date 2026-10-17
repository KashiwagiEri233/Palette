import { it, expect } from 'vitest'
import { createServer } from 'node:http'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { PiAgentService, validateAgentConfiguration } from '../src/main/pi-agent'
import { AgentClient, type AgentReply } from '../src/main/agent-client'
import type { AgentConfiguration, AgentEvent } from '../src/shared/agent'
import type { Conversation } from '../src/shared/workspace'
import { atomicJson } from '../src/main/agent-storage'

const encryption = { available: () => true, encrypt: (text: string) => Buffer.from(text.split('').reverse().join('')), decrypt: (bytes: Buffer) => bytes.toString().split('').reverse().join('') }
const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP1sAAAAASUVORK5CYII=', 'base64')
class FakeClient extends AgentClient {
  readonly calls: string[] = []
  unknownAction = false
  constructor() { super({ address: 'unused', generation: '', token: '', operation_timeout_seconds: 1 }) }
  override async request(method: string, _vm: string, parameters: Record<string, unknown> = {}, _claim?: string, signal?: AbortSignal): Promise<AgentReply> {
    signal?.throwIfAborted()
    this.calls.push(method)
    if (method === 'status') return { result: { ok: true, status: { instance_id: 'instance' } }, image: Buffer.alloc(0) }
    if (method === 'driver_acquire') return { result: { claim_token: 'a'.repeat(48) }, image: Buffer.alloc(0) }
    if (method === 'driver_release') return { result: { release_confirmed: true }, image: Buffer.alloc(0) }
    if (method === 'act') expect(parameters).toMatchObject({ instance_id: 'instance', frame_id: 'frame', action: { type: 'click', x: 0, y: 0 } })
    if (method === 'act' && this.unknownAction) return { result: { ok: false, error: { code: 'ACTION_OUTCOME_UNKNOWN', message: 'unknown' } }, image: Buffer.alloc(0) }
    return { result: { ok: true, frame: { instance_id: 'instance', frame_id: 'frame', width: 1, height: 1 } }, image: pixel }
  }
}

async function environment() {
  const root = await mkdtemp(join(tmpdir(), 'palette-pi-sdk-'))
  const requests: Record<string, unknown>[] = []
  let slow = false
  let fail = false
  let measuredUsage = false
  const server = createServer(async (request, response) => {
    let body = ''
    for await (const data of request) body += data
    const payload = JSON.parse(body)
    requests.push(payload)
    expect(request.headers.authorization).toBe('Bearer test-secret')
    if (fail) { response.writeHead(401, { 'content-type': 'application/json' }); response.end(JSON.stringify({ error: { message: 'test-secret unauthorized' } })); return }
    response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
    const chunk = (delta: unknown, finishReason: string | null = null) => response.write(`data: ${JSON.stringify({ id: 'chatcmpl-palette', object: 'chat.completion.chunk', created: 1, model: 'fixture', choices: [{ index: 0, delta, finish_reason: finishReason }], ...(measuredUsage && finishReason === 'stop' ? { usage: { prompt_tokens: 2048, completion_tokens: 128, total_tokens: 2176 } } : {}) })}\n\n`)
    chunk({ role: 'assistant' })
    if (slow) { chunk({ content: '开始执行' }); return }
    const tools = payload.messages.filter((message: { role: string }) => message.role === 'tool').length
    const visual = payload.messages.some((message: { role: string; content: unknown }) => message.role === 'user' && JSON.stringify(message.content).includes('visual'))
    if (visual && tools < 2) {
      const name = tools === 0 ? 'observe' : 'act'
      chunk({ tool_calls: [{ index: 0, id: `call-${tools}`, type: 'function', function: { name, arguments: JSON.stringify(tools === 0 ? {} : { instance_id: 'instance', frame_id: 'frame', action: { type: 'click', x: 0, y: 0 } }) } }] })
      chunk({}, 'tool_calls')
    } else {
      chunk({ content: '任务' })
      chunk({ content: '完成' })
      chunk({}, 'stop')
    }
    response.write('data: [DONE]\n\n')
    response.end()
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address() as { port: number }
  const id = randomUUID()
  const conversation: Conversation = { id, vm_id: 'vm', title: 'Test', draft: '', pinned: false, archived: false, revision: 1, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() }
  const conversations = [conversation]
  const client = new FakeClient()
  const events: AgentEvent[] = []
  const create = (scope = 'configuration') => new PiAgentService(root, join(root, scope), () => root, encryption, async () => conversations, event => events.push(event), async () => client)
  const service = create()
  const configuration: AgentConfiguration = { providerId: 'custom', modelId: 'fixture', api: 'openai-completions', baseUrl: `http://127.0.0.1:${address.port}/v1`, custom: true, contextWindow: 128000, maxTokens: 1024, vision: true, reasoning: false, thinkingLevel: 'off', maxSteps: 100 }
  await service.updateSettings(configuration)
  await service.configureCredential('palette-custom', 'test-secret')
  return { root, id, service, create, conversations, client, requests, events, configuration, slow: () => { slow = true }, fail: () => { fail = true }, measure: () => { measuredUsage = true },
    close: async () => { await service.close(); server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); await rm(root, { recursive: true, force: true }) } }
}
async function settled(service: PiAgentService, id: string) {
  for (let i = 0; i < 250; i++) {
    const snapshot = await service.snapshot('vm', id)
    if (snapshot.view.run && !['starting', 'running', 'stopping'].includes(snapshot.view.run.state)) return snapshot
    await new Promise(resolve => setTimeout(resolve, 20))
  }
  throw new Error('Agent did not settle')
}

it('runs the real SDK through VM tools, persists history and reopens its tree', async () => {
  const test = await environment()
  try {
    test.measure()
    expect((await test.service.snapshot('vm', test.id)).view.contextUsage).toEqual({ tokens: 0, contextWindow: 128000, percent: 0 })
    const requestId = randomUUID()
    const accepted = await test.service.start('vm', test.id, 'visual task', requestId)
    expect(await test.service.start('vm', test.id, 'visual task', requestId)).toEqual(accepted)
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('completed')
    expect(test.client.calls).toContain('observe_image')
    expect(test.client.calls).toContain('act')
    expect(test.client.calls.at(-1)).toBe('driver_release')
    expect(test.requests).toHaveLength(3)
    expect(result.view.messages.filter(message => message.role === 'assistant').map(message => message.text)).toContain('任务完成')
    expect(result.view.run?.stepCount).toBe(2)
    expect(result.view.contextUsage).toMatchObject({ tokens: 2176, contextWindow: 128000 })
    expect(result.view.contextUsage!.percent).toBeCloseTo(1.7)
    const names = ((test.requests[0].tools ?? []) as { function: { name: string } }[]).map(tool => tool.function.name)
    expect(names.sort()).toEqual(['act', 'observe', 'release_inputs', 'status', 'wait'])
    expect(JSON.stringify(result)).not.toContain('test-secret')
    const directory = join(test.service.directory, 'sessions', test.id)
    const files = (await readdir(directory)).filter(file => file.endsWith('.jsonl'))
    expect(files).toHaveLength(1)
    const persisted = await readFile(join(directory, files[0]), 'utf8')
    expect(persisted).not.toContain('test-secret')
    expect(persisted).toContain('visual task')
    await test.service.close()
    const reopened = test.create()
    try {
      const snapshot = await reopened.snapshot('vm', test.id)
      expect(snapshot.view.messages).toEqual(result.view.messages)
      expect(snapshot.view.contextUsage).toEqual(result.view.contextUsage)
      expect(snapshot.view.tree.length).toBeGreaterThan(1)
      const user = snapshot.view.tree.find(entry => entry.label.startsWith('用户'))!
      const branch = await reopened.navigate('vm', test.id, user.id)
      expect(branch.editorText).toBe('visual task')
      expect(test.requests).toHaveLength(3)
    } finally { await reopened.close() }
  } finally { await test.close() }
}, 20000)

it('stops a slow response, rejects same-VM competition and never dispatches late tools', async () => {
  const test = await environment()
  try {
    test.slow()
    await test.service.start('vm', test.id, 'slow task', randomUUID())
    test.conversations.push({ ...test.conversations[0], id: randomUUID() })
    await expect(test.service.start('vm', test.conversations[1].id, 'competing task', randomUUID())).rejects.toThrow('已有运行中的任务')
    await new Promise(resolve => setTimeout(resolve, 80))
    const [result] = await Promise.all([test.service.stop('vm', test.id), test.service.stop('vm', test.id)])
    expect(result.view.run?.state).toBe('cancelled')
    expect(test.client.calls).not.toContain('act')
    expect(test.client.calls).toContain('driver_release')
    expect(test.client.calls.filter(method => method === 'driver_release')).toHaveLength(1)
    expect(result.view.messages.some(message => message.text === 'slow task')).toBe(true)
  } finally { await test.close() }
}, 20000)

it('reports provider failures while preserving the prompt and removing echoed credentials', async () => {
  const test = await environment()
  try {
    test.fail()
    await test.service.start('vm', test.id, 'keep this prompt', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('failed')
    expect(result.view.messages.some(message => message.text === 'keep this prompt')).toBe(true)
    const directory = join(test.service.directory, 'sessions', test.id)
    const file = (await readdir(directory)).find(file => file.endsWith('.jsonl'))!
    expect(await readFile(join(directory, file), 'utf8')).not.toContain('test-secret')
  } finally { await test.close() }
}, 20000)

it('validates custom endpoints and bounds before saving configuration', () => {
  expect(() => validateAgentConfiguration({})).toThrow('设置无效')
})

it.each(['limit', 'unknown'])('halts the SDK tool loop on %s without replaying input', async mode => {
  const test = await environment()
  try {
    if (mode === 'limit') await test.service.updateSettings({ ...test.configuration, maxSteps: 1 })
    else test.client.unknownAction = true
    await test.service.start('vm', test.id, 'visual task', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('cancelled')
    expect(result.view.run?.error).toContain(mode === 'limit' ? '最大工具调用次数' : '操作结果未知')
    expect(test.client.calls.filter(method => method === 'act')).toHaveLength(mode === 'limit' ? 0 : 1)
  } finally { await test.close() }
}, 20000)

it('compacts real SDK history and retains earlier conversation branches', async () => {
  const test = await environment()
  try {
    await test.service.updateSettings({ ...test.configuration, vision: false, contextWindow: 12000 })
    for (let i = 0; i < 3; i++) {
      await test.service.start('vm', test.id, `Message ${i}: ${'context '.repeat(1000)}`, randomUUID())
      expect((await settled(test.service, test.id)).view.run?.state).toBe('completed')
    }
    const before = await test.service.snapshot('vm', test.id)
    const result = await test.service.compact('vm', test.id)
    expect(result.view.messages.some(message => message.role === 'system' && message.text === '上下文已压缩')).toBe(true)
    expect(result.view.tree.length).toBeGreaterThan(before.view.tree.length)
    expect(result.view.contextUsage).toEqual({ tokens: null, contextWindow: 12000, percent: null })
    expect(test.client.calls).toHaveLength(0)
    const branch = await test.service.navigate('vm', test.id, before.view.tree.find(entry => entry.label.startsWith('用户'))!.id)
    expect(branch.editorText).toContain('Message 0')
    await test.service.start('vm', test.id, 'new branch', randomUUID())
    const alternate = await settled(test.service, test.id)
    expect(alternate.view.tree.some(entry => !entry.active)).toBe(true)
    expect(alternate.view.messages.some(message => message.text === 'new branch')).toBe(true)
  } finally { await test.close() }
}, 20000)

it('recovers interrupted runs without executing and isolates configuration roots', async () => {
  const test = await environment()
  try {
    await test.service.start('vm', test.id, 'persisted', randomUUID())
    await settled(test.service, test.id)
    await test.service.close()
    await atomicJson(join(test.service.directory, 'sessions', test.id, 'run.json'), { run: { id: randomUUID(), state: 'running', stepCount: 1 } })
    const reopened = test.create()
    const isolated = test.create('other-configuration')
    try {
      await expect(reopened.snapshot('vm', '..')).rejects.toThrow('会话参数无效')
      const calls = test.requests.length
      const snapshot = await reopened.snapshot('vm', test.id)
      expect(snapshot.view.run?.state).toBe('interrupted')
      expect(test.requests).toHaveLength(calls)
      expect((await isolated.snapshot('vm', test.id)).view.messages).toHaveLength(0)
      expect((await isolated.settings()).credentialConfigured).toBe(false)
    } finally { await reopened.close(); await isolated.close() }
  } finally { await test.close() }
}, 20000)
