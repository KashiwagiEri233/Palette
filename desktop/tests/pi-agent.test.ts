import { it, expect, vi } from 'vitest'
import { createServer } from 'node:http'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { PiAgentService, validateAgentConfiguration } from '../src/main/pi-agent'
import { AgentClient, type AgentReply } from '../src/main/agent-client'
import type { AgentConfiguration, AgentEvent, AgentRunView } from '../src/shared/agent'
import type { Conversation } from '../src/shared/workspace'
import { atomicJson } from '../src/main/agent-storage'

const encryption = { available: () => true, encrypt: (text: string) => Buffer.from(text.split('').reverse().join('')), decrypt: (bytes: Buffer) => bytes.toString().split('').reverse().join('') }
const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP1sAAAAASUVORK5CYII=', 'base64')
class FakeClient extends AgentClient {
  readonly calls: string[] = []
  readonly requests: { method: string; parameters: Record<string, unknown>; claim?: string }[] = []
  unknownAction = false
  unknownEnhancement = false
  actionResult?: Record<string, unknown>
  constructor() { super({ address: 'unused', generation: '', token: '', operation_timeout_seconds: 1 }) }
  override async request(method: string, _vm: string, parameters: Record<string, unknown> = {}, _claim?: string, signal?: AbortSignal): Promise<AgentReply> {
    signal?.throwIfAborted()
    this.calls.push(method)
    this.requests.push({ method, parameters, claim: _claim })
    if (method === 'status') return { result: { ok: true, status: { instance_id: 'instance' } }, image: Buffer.alloc(0) }
    if (method === 'driver_acquire') return { result: { claim_token: 'a'.repeat(48) }, image: Buffer.alloc(0) }
    if (method === 'driver_release') return { result: { release_confirmed: true }, image: Buffer.alloc(0) }
    if (method === 'act') expect(parameters).toMatchObject({ instance_id: 'instance', frame_id: 'frame', action: { type: 'click', x: 0, y: 0 } })
    if (method === 'act' && this.unknownAction) return { result: { ok: false, error: { code: 'ACTION_OUTCOME_UNKNOWN', message: 'unknown' } }, image: Buffer.alloc(0) }
    if (method === 'act' && this.actionResult) return { result: this.actionResult, image: Buffer.alloc(0) }
    if (['guest_input_text', 'guest_enhancement'].includes(method)) return { result: { ok: !this.unknownEnhancement, effect_state: this.unknownEnhancement ? 'unknown' : 'submitted', data: { transfer_id: 'transfer', count: 3 } }, image: Buffer.alloc(0) }
    return { result: { ok: true, frame: { instance_id: 'instance', frame_id: 'frame', width: 1, height: 1 } }, image: pixel }
  }
}

async function environment() {
  const root = await mkdtemp(join(tmpdir(), 'palette-pi-sdk-'))
  const requests: Record<string, unknown>[] = []
  let slow = false
  let fail = false
  let failureStatus = 401
  let failureMessage = 'test-secret unauthorized'
  let measuredUsage = false
  let script: { name: string; arguments: Record<string, unknown> }[] | undefined
  const server = createServer(async (request, response) => {
    let body = ''
    for await (const data of request) body += data
    const payload = JSON.parse(body)
    requests.push(payload)
    expect(request.headers.authorization).toBe('Bearer test-secret')
    if (fail) { response.writeHead(failureStatus, { 'content-type': 'application/json' }); response.end(JSON.stringify({ error: { message: failureMessage } })); return }
    const invalidItems = (value: unknown): boolean => {
      if (Array.isArray(value)) return value.some(invalidItems)
      if (!value || typeof value !== 'object') return false
      const object = value as Record<string, unknown>
      if ('items' in object && typeof object.items !== 'boolean' && (!object.items || typeof object.items !== 'object' || Array.isArray(object.items))) return true
      return Object.values(object).some(invalidItems)
    }
    // Mirror the endpoint's JSON Schema meta-validation, not just its SSE response.
    if (invalidItems(payload.tools)) {
      response.writeHead(400, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ error: { message: 'Invalid schema for function: items must be boolean or object' } }))
      return
    }
    response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
    const chunk = (delta: unknown, finishReason: string | null = null) => response.write(`data: ${JSON.stringify({ id: 'chatcmpl-palette', object: 'chat.completion.chunk', created: 1, model: 'fixture', choices: [{ index: 0, delta, finish_reason: finishReason }], ...(measuredUsage && finishReason === 'stop' ? { usage: { prompt_tokens: 2048, completion_tokens: 128, total_tokens: 2176 } } : {}) })}\n\n`)
    chunk({ role: 'assistant' })
    if (slow) { chunk({ content: '开始执行' }); return }
    const tools = payload.messages.filter((message: { role: string }) => message.role === 'tool').length
    const visual = payload.messages.some((message: { role: string; content: unknown }) => message.role === 'user' && JSON.stringify(message.content).includes('visual'))
    if (script && tools < script.length) {
      chunk({ tool_calls: [{ index: 0, id: `script-${tools}`, type: 'function', function: { name: script[tools].name, arguments: JSON.stringify(script[tools].arguments) } }] })
      chunk({}, 'tool_calls')
    } else if (visual && tools < 2) {
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
  const configuration: AgentConfiguration = { providerId: 'custom-fixture', modelId: 'fixture', api: 'openai-completions', baseUrl: `http://127.0.0.1:${address.port}/v1`, custom: true, contextWindow: 128000, maxTokens: 1024, vision: true, reasoning: false, thinkingLevel: 'off', maxSteps: 100 }
  await service.saveProvider({ id: configuration.providerId, name: 'Fixture', api: configuration.api, baseUrl: configuration.baseUrl, models: [{ ...configuration, id: configuration.modelId, name: configuration.modelId }] }, 'test-secret')
  await service.updateSettings(configuration)
  return { root, id, service, create, conversations, client, requests, events, configuration, script: (steps: NonNullable<typeof script>) => { script = steps }, slow: () => { slow = true }, fail: (message?: string, status = 401) => { fail = true; failureStatus = status; failureMessage = message ?? failureMessage }, measure: () => { measuredUsage = true },
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
    expect(names.sort()).toEqual(['act', 'app', 'clipboard_get', 'clipboard_set', 'display_resize', 'file_close', 'file_open', 'file_read', 'file_write', 'filesystem', 'input_text', 'multi_edit', 'multi_select', 'observe', 'powershell', 'process', 'registry', 'release_inputs', 'snapshot', 'status', 'sync_time', 'wait'])
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

it.each([true, false])('keeps a run active until control release completes (confirmed: %s)', async confirmed => {
  const test = await environment()
  let enteredRelease!: () => void
  let finishRelease!: () => void
  const entered = new Promise<void>(resolve => { enteredRelease = resolve })
  const blocked = new Promise<void>(resolve => { finishRelease = resolve })
  const request = test.client.request.bind(test.client)
  let releases = 0
  vi.spyOn(test.client, 'request').mockImplementation(async (...args) => {
    if (args[0] === 'driver_release' && ++releases === 1) {
      enteredRelease()
      await blocked
      const reply = await request(...args)
      return { ...reply, result: { release_confirmed: confirmed } }
    }
    return request(...args)
  })
  try {
    await test.service.start('vm', test.id, 'complete then release', randomUUID())
    await entered
    expect((await test.service.snapshot('vm', test.id)).view.run?.state).toBe('running')
    await expect(test.service.start('vm', test.id, 'next task', randomUUID())).rejects.toThrow('已有运行中的任务')
    await expect(test.service.compact('vm', test.id)).rejects.toThrow('请先停止当前任务')
    finishRelease()
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe(confirmed ? 'completed' : 'failed')
    if (!confirmed) expect(result.view.run?.error).toContain('输入释放未确认')
    await expect(test.service.start('vm', test.id, 'next task', randomUUID())).resolves.toHaveProperty('runId')
    expect((await settled(test.service, test.id)).view.run?.state).toBe('completed')
  } finally { finishRelease(); await test.close() }
}, 20000)

it('keeps a text-only run active until its final state is saved', async () => {
  const test = await environment()
  let enteredSave!: () => void
  let finishSave!: () => void
  const entered = new Promise<void>(resolve => { enteredSave = resolve })
  const blocked = new Promise<void>(resolve => { finishSave = resolve })
  const internals = test.service as unknown as { saveRun(record: { run: AgentRunView | null }, run?: AgentRunView | null): Promise<void> }
  const save = internals.saveRun.bind(test.service)
  let blockedOnce = false
  vi.spyOn(internals, 'saveRun').mockImplementation(async (record, run) => {
    if (!blockedOnce && (run ?? record.run)?.state === 'completed') {
      blockedOnce = true
      enteredSave()
      await blocked
    }
    return save(record, run)
  })
  try {
    await test.service.saveProvider({ id: test.configuration.providerId, name: 'Fixture', api: test.configuration.api, baseUrl: test.configuration.baseUrl, models: [{ ...test.configuration, id: test.configuration.modelId, name: test.configuration.modelId, vision: false }] })
    await test.service.start('vm', test.id, 'text-only task', randomUUID())
    await entered
    expect((await test.service.snapshot('vm', test.id)).view.run?.state).toBe('running')
    await expect(test.service.start('vm', test.id, 'next task', randomUUID())).rejects.toThrow('已有运行中的任务')
    finishSave()
    expect((await settled(test.service, test.id)).view.run?.state).toBe('completed')
    const saved = JSON.parse(await readFile(join(test.service.directory, 'sessions', test.id, 'run.json'), 'utf8'))
    expect(saved.run.state).toBe('completed')
    await expect(test.service.start('vm', test.id, 'next task', randomUUID())).resolves.toHaveProperty('runId')
    expect((await settled(test.service, test.id)).view.run?.state).toBe('completed')
  } finally { finishSave(); await test.close() }
}, 20000)

it('stops a run while its control release is pending without releasing twice', async () => {
  const test = await environment()
  let enteredRelease!: () => void
  let finishRelease!: () => void
  let enteredAbort!: () => void
  let finishAbort!: () => void
  const entered = new Promise<void>(resolve => { enteredRelease = resolve })
  const blocked = new Promise<void>(resolve => { finishRelease = resolve })
  const abortEntered = new Promise<void>(resolve => { enteredAbort = resolve })
  const abortBlocked = new Promise<void>(resolve => { finishAbort = resolve })
  const request = test.client.request.bind(test.client)
  vi.spyOn(test.client, 'request').mockImplementation(async (...args) => {
    if (args[0] === 'driver_release') { enteredRelease(); await blocked }
    return request(...args)
  })
  try {
    await test.service.start('vm', test.id, 'complete then stop', randomUUID())
    await entered
    const internals = test.service as unknown as { records: Map<string, { run: AgentRunView | null; session: { abort(): Promise<void> } }> }
    const record = internals.records.get(test.id)!
    const abort = record.session.abort.bind(record.session)
    vi.spyOn(record.session, 'abort').mockImplementation(async () => { enteredAbort(); await abortBlocked; await abort() })
    const stopping = test.service.stop('vm', test.id)
    await vi.waitFor(async () => expect((await test.service.snapshot('vm', test.id)).view.run?.state).toBe('stopping'))
    await expect(test.service.start('vm', test.id, 'next task', randomUUID())).rejects.toThrow('已有运行中的任务')
    finishRelease()
    await abortEntered
    await vi.waitFor(() => expect(record.run?.state).toBe('cancelled'))
    expect((await test.service.snapshot('vm', test.id)).view.run?.state).toBe('stopping')
    const other = { ...test.conversations[0], id: randomUUID() }
    test.conversations.push(other)
    await expect(test.service.start('vm', other.id, 'competing task', randomUUID())).rejects.toThrow('已有运行中的任务')
    finishAbort()
    expect((await stopping).view.run?.state).toBe('cancelled')
    expect(test.client.calls.filter(method => method === 'driver_release')).toHaveLength(1)
    await expect(test.service.start('vm', test.id, 'next task', randomUUID())).resolves.toHaveProperty('runId')
    expect((await settled(test.service, test.id)).view.run?.state).toBe('completed')
  } finally { finishRelease(); finishAbort(); await test.close() }
}, 20000)

it('stops a slow response, rejects same-VM competition and never dispatches late tools', async () => {
  const test = await environment()
  try {
    test.slow()
    await test.service.start('vm', test.id, 'slow task', randomUUID())
    await expect(test.service.saveProvider({ id: test.configuration.providerId, name: 'Busy', api: test.configuration.api, baseUrl: test.configuration.baseUrl, models: [] })).rejects.toThrow('停止运行中的任务')
    await expect(test.service.deleteProvider(test.configuration.providerId)).rejects.toThrow('停止运行中的任务')
    await expect(test.service.configureCredential(test.configuration.providerId, null)).rejects.toThrow('停止运行中的任务')
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
    expect(result.view.run?.error).toContain('HTTP 401')
    expect(result.view.run?.error).not.toContain('test-secret')
    expect(result.view.messages.some(message => message.text === 'keep this prompt')).toBe(true)
    const directory = join(test.service.directory, 'sessions', test.id)
    const file = (await readdir(directory)).find(file => file.endsWith('.jsonl'))!
    expect(await readFile(join(directory, file), 'utf8')).not.toContain('test-secret')
  } finally { await test.close() }
}, 20000)

it('reports a rejected tool schema as an application compatibility error and redacts persisted details', async () => {
  const test = await environment()
  try {
    test.fail("Invalid schema for function 'app': test-secret items is not of types boolean, object", 400)
    await test.service.start('vm', test.id, 'keep this task', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.error).toContain('工具参数格式不兼容')
    expect(result.view.run?.error).toContain("'app'")
    expect(result.view.run?.error).not.toContain('test-secret')
    const run = await readFile(join(test.service.directory, 'sessions', test.id, 'run.json'), 'utf8')
    expect(run).toContain('HTTP 400')
    expect(run).not.toContain('test-secret')
    expect(test.client.calls.at(-1)).toBe('driver_release')
    const diagnostics = await test.service.diagnostics('vm', test.id)
    expect(diagnostics).toMatchObject({ vmId: 'vm', conversationId: test.id, model: { api: 'openai-completions' }, run: { state: 'failed' }, modelError: expect.stringContaining('Invalid schema') })
    expect(JSON.stringify(diagnostics)).not.toContain('test-secret')
    expect(JSON.stringify(diagnostics)).not.toContain('keep this task')
    const metadata = JSON.parse(run)
    metadata.run.error = '模型请求失败，请检查端点、密钥和模型配置'
    await atomicJson(join(test.service.directory, 'sessions', test.id, 'run.json'), metadata)
    const reopened = test.create()
    try {
      expect((await reopened.snapshot('vm', test.id)).view.run?.error).toContain('工具参数格式不兼容')
    } finally { await reopened.close() }
  } finally { await test.close() }
}, 20000)

it('sends built-in DeepSeek Flash tool declarations accepted by schema-validating endpoints', async () => {
  const test = await environment()
  const fetch = globalThis.fetch.bind(globalThis)
  const models = (await test.service.settings()).models
  const model = models.find(model => model.providerId === 'deepseek' && model.id === 'deepseek-flash')!
  expect(model).toBeDefined()
  const intercept = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url)
    if (url.origin !== new URL(model.baseUrl).origin) throw new Error('Unexpected model network request')
    const target = test.configuration.baseUrl + '/chat/completions'
    return fetch(input instanceof Request ? new Request(target, input) : target, init)
  })
  try {
    await test.service.configureCredential('deepseek', 'test-secret')
    await test.service.updateSettings({ ...test.configuration, providerId: 'deepseek', modelId: 'deepseek-flash', custom: false, thinkingLevel: 'max' })
    await test.service.start('vm', test.id, 'test tool schema compatibility', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('completed')
    expect(test.requests[0].model).toBe('deepseek-flash')
    expect((test.requests[0].tools as { function: { name: string } }[]).some(tool => tool.function.name === 'app')).toBe(true)
    expect(result.view.run?.error).toBeUndefined()
  } finally { intercept.mockRestore(); await test.close() }
}, 20000)

it('validates custom endpoints and bounds before saving configuration', () => {
  expect(() => validateAgentConfiguration({})).toThrow('设置无效')
})

it.each(['limit', 'unknown', 'timeout'])('halts the SDK tool loop on %s without replaying input', async mode => {
  const test = await environment()
  try {
    if (mode === 'limit') await test.service.updateSettings({ ...test.configuration, maxSteps: 1 })
    else if (mode === 'unknown') test.client.unknownAction = true
    else test.client.actionResult = { ok: false, effect_state: 'unknown', error: { code: 'TIMEOUT', message: 'QMP confirmation timed out' } }
    await test.service.start('vm', test.id, 'visual task', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('cancelled')
    expect(result.view.run?.error).toContain(mode === 'limit' ? '最大工具调用次数' : '操作结果未知')
    expect(test.client.calls.filter(method => method === 'act')).toHaveLength(mode === 'limit' ? 0 : 1)
    expect(test.requests).toHaveLength(2)
    expect(test.client.calls.filter(method => method === 'driver_release')).toHaveLength(1)
    expect(test.client.calls.at(-1)).toBe('driver_release')
  } finally { await test.close() }
}, 20000)

it('lets the SDK handle a failure that explicitly sent no input', async () => {
  const test = await environment()
  try {
    test.client.actionResult = { ok: false, effect_state: 'not_sent', error: { code: 'STALE_FRAME', message: 'Observe again' } }
    await test.service.start('vm', test.id, 'visual task', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('completed')
    expect(result.view.run?.error).toBeUndefined()
    expect(test.requests).toHaveLength(3)
    expect(test.client.calls.filter(method => method === 'act')).toHaveLength(1)
    expect(test.client.calls.filter(method => method === 'driver_release')).toHaveLength(1)
  } finally { await test.close() }
}, 20000)

it.each(['archive', 'delete'])('does not stop another session on the same VM during %s', async operation => {
  const test = await environment()
  try {
    const old = { ...test.conversations[0], id: randomUUID() }
    test.conversations.push(old)
    await test.service.snapshot('vm', old.id)
    test.slow()
    const accepted = await test.service.start('vm', test.id, 'slow task', randomUUID())
    await vi.waitFor(() => expect(test.requests).toHaveLength(1))
    await test.service.stop('vm', old.id)
    if (operation === 'archive') old.archived = true
    else {
      test.conversations.splice(test.conversations.indexOf(old), 1)
      await test.service.remove('vm', old.id)
      await expect(readdir(join(test.service.directory, 'sessions', old.id))).rejects.toMatchObject({ code: 'ENOENT' })
    }
    expect((await test.service.snapshot('vm', test.id)).view.run).toMatchObject({ id: accepted.runId, state: 'running' })
    expect(test.client.calls).not.toContain('driver_release')
    expect(test.client.calls.filter(method => method === 'driver_acquire')).toHaveLength(1)
    if (operation === 'archive') {
      await expect(test.service.start('vm', old.id, 'archived task', randomUUID())).rejects.toThrow('已归档对话为只读')
    }
  } finally { await test.close() }
}, 20000)

it.each(['archive', 'delete', 'remove'])('stops and releases only the target running session on %s', async operation => {
  const test = await environment()
  try {
    test.slow()
    await test.service.start('vm', test.id, 'slow task', randomUUID())
    await vi.waitFor(() => expect(test.requests).toHaveLength(1))
    if (operation !== 'remove') {
      expect((await test.service.stop('vm', test.id)).view.run?.state).toBe('cancelled')
      if (operation === 'archive') test.conversations[0].archived = true
      else test.conversations.splice(0, 1)
    }
    if (operation !== 'archive') {
      await test.service.remove('vm', test.id)
      await expect(readdir(join(test.service.directory, 'sessions', test.id))).rejects.toMatchObject({ code: 'ENOENT' })
    } else {
      expect((await test.service.snapshot('vm', test.id)).view.run?.state).toBe('cancelled')
    }
    expect(test.client.calls.filter(method => method === 'driver_release')).toHaveLength(1)
    expect(test.client.calls).not.toContain('act')
    expect(test.requests).toHaveLength(1)
  } finally { await test.close() }
}, 20000)

it('rejects removing a session owned by another VM without stopping its task', async () => {
  const test = await environment()
  try {
    test.slow()
    await test.service.start('vm', test.id, 'slow task', randomUUID())
    await expect(test.service.remove('other-vm', test.id)).rejects.toThrow('对话不属于此虚拟机')
    expect((await test.service.snapshot('vm', test.id)).view.run?.state).toBe('running')
    expect(test.client.calls).not.toContain('driver_release')
  } finally { await test.close() }
}, 20000)

it.each(['stop', 'remove'] as const)('waits for a starting session before %s and releases its claim', async operation => {
  const test = await environment()
  let acquireStarted!: () => void
  let finishAcquire!: () => void
  const entered = new Promise<void>(resolve => { acquireStarted = resolve })
  const blocked = new Promise<void>(resolve => { finishAcquire = resolve })
  const request = test.client.request.bind(test.client)
  vi.spyOn(test.client, 'request').mockImplementation(async (...args) => {
    if (args[0] === 'driver_acquire') { acquireStarted(); await blocked }
    return request(...args)
  })
  try {
    test.slow()
    const starting = test.service.start('vm', test.id, 'slow task', randomUUID())
    await entered
    const stopping = test.service[operation]('vm', test.id)
    await new Promise(resolve => setImmediate(resolve))
    expect((await test.service.snapshot('vm', test.id)).view.run?.state).toBe('starting')
    finishAcquire()
    await starting
    await stopping
    expect(test.client.calls.filter(method => method === 'driver_release')).toHaveLength(1)
    expect(test.client.calls).not.toContain('act')
    if (operation === 'stop') expect((await test.service.snapshot('vm', test.id)).view.run?.state).toBe('cancelled')
    else await expect(readdir(join(test.service.directory, 'sessions', test.id))).rejects.toMatchObject({ code: 'ENOENT' })
  } finally { finishAcquire(); await test.close() }
}, 20000)

it('compacts real SDK history and retains earlier conversation branches', async () => {
  const test = await environment()
  try {
    await test.service.saveProvider({ id: test.configuration.providerId, name: 'Fixture', api: test.configuration.api, baseUrl: test.configuration.baseUrl, models: [{ ...test.configuration, id: test.configuration.modelId, name: test.configuration.modelId, vision: false, contextWindow: 12000 }] })
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


it('lets the real SDK input Chinese, use clipboard and transfer files directly under its claim', async () => {
  const test = await environment()
  const identity = { instance_id: 'instance', boot_epoch: 1 }
  try {
    test.script([
      { name: 'input_text', arguments: { ...identity, frame_id: 'frame', text: '中文输入\n第二行 😀' } },
      { name: 'clipboard_get', arguments: identity },
      { name: 'clipboard_set', arguments: { ...identity, text: '客户机剪贴板' } },
      { name: 'file_open', arguments: { ...identity, path: '/home/guest/文件.bin', mode: 'w' } },
      { name: 'file_write', arguments: { ...identity, transfer_id: 'transfer', data_base64: 'AP8B' } },
      { name: 'file_close', arguments: { ...identity, transfer_id: 'transfer' } },
      { name: 'file_open', arguments: { ...identity, path: '/home/guest/文件.bin', mode: 'r' } },
      { name: 'file_read', arguments: { ...identity, transfer_id: 'transfer', count: 3 } },
      { name: 'file_close', arguments: { ...identity, transfer_id: 'transfer' } }
    ])
    await test.service.start('vm', test.id, 'enhancement task', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('completed')
    const requests = test.client.requests.filter(request => ['guest_input_text', 'guest_enhancement'].includes(request.method))
    expect(requests).toHaveLength(9)
    expect(requests.every(request => request.claim === 'a'.repeat(48))).toBe(true)
    expect(requests[0].parameters).toMatchObject({ ...identity, text: '中文输入\n第二行 😀', method: 'clipboard_paste' })
    expect(requests[3].parameters).toMatchObject({ operation: 'file.open', parameters: { path: '/home/guest/文件.bin', mode: 'w' } })
    expect(requests[4].parameters).toMatchObject({ operation: 'file.write', parameters: { transfer_id: 'transfer', data_base64: 'AP8B' } })
    expect(new Set(requests.map(request => request.parameters.request_id)).size).toBe(9)
    expect(test.client.calls.at(-1)).toBe('driver_release')
  } finally { await test.close() }
}, 20000)

it('stops the SDK after an unknown enhancement result without issuing the next write', async () => {
  const test = await environment()
  try {
    test.client.unknownEnhancement = true
    test.script([
      { name: 'clipboard_set', arguments: { instance_id: 'instance', boot_epoch: 1, text: '中文' } },
      { name: 'clipboard_set', arguments: { instance_id: 'instance', boot_epoch: 1, text: '重复' } }
    ])
    await test.service.start('vm', test.id, 'enhancement task', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('cancelled')
    expect(test.client.calls.filter(method => method === 'guest_enhancement')).toHaveLength(1)
    expect(test.client.calls.at(-1)).toBe('driver_release')
  } finally { await test.close() }
}, 20000)

it('runs Windows management and batch tools through the bound guest claim with frame identity outside parameters', async () => {
  const test = await environment()
  const identity = { instance_id: 'instance', boot_epoch: 1 }
  const steps = [
    { name: 'filesystem', parameters: { mode: 'write', path: 'C:\\Users\\Guest\\中文.txt', content: '第一行\n第二行 😀', append: true, encoding: 'utf-8' } },
    { name: 'filesystem', parameters: { mode: 'search', path: 'C:\\Users\\Guest', recursive: true, pattern: '*.txt', offset: 20, limit: 10 } },
    { name: 'powershell', parameters: { command: 'Get-Location', cwd: 'C:\\Users\\Guest', timeout_seconds: 45 } },
    { name: 'process', parameters: { mode: 'list', name: 'notepad', sort_by: 'memory', limit: 10 } },
    { name: 'process', parameters: { mode: 'kill', pid: 321, force: false } },
    { name: 'registry', parameters: { mode: 'set', path: 'HKCU:\\Software\\Palette', name: 'Names', value: ['中文', 'Guest'], type: 'MultiString' } },
    { name: 'registry', parameters: { mode: 'set', path: 'HKCU:\\Software\\Palette', name: 'Bytes', value: [0, 128, 255], type: 'Binary' } },
    { name: 'app', parameters: { mode: 'launch_executable', executable: 'C:\\Windows\\System32\\notepad.exe', args: ['C:\\Users\\Guest\\中文.txt'], cwd: 'C:\\Users\\Guest' } },
    { name: 'app', parameters: { mode: 'resize', window_id: 'window-1', window_loc: [-10, 20], window_size: [800, 600] } },
    { name: 'snapshot', parameters: { max_elements: 250 } },
    { name: 'multi_select', parameters: { snapshot_id: 'snapshot-1', labels: [1, 2], press_ctrl: true }, frame_id: 'frame' },
    { name: 'multi_select', parameters: { locs: [[10, 20], [30, 40]], press_ctrl: false }, frame_id: 'frame-2' },
    { name: 'multi_edit', parameters: { snapshot_id: 'snapshot-2', labels: [[3, '字段 😀']], clear: true }, frame_id: 'frame-3' },
    { name: 'multi_edit', parameters: { locs: [[100, 200, '中文\n多行']], clear: false }, frame_id: 'frame-4' }
  ]
  try {
    const settings = await test.service.settings()
    expect(settings.guestProgramming).toMatchObject({ availability: 'per_vm' })
    expect(settings.guestProgramming.reason).toContain('虚拟机状态')
    test.script(steps.map(step => ({ name: step.name, arguments: { ...identity, ...step.parameters, ...(step.frame_id ? { frame_id: step.frame_id } : {}) } })))
    await test.service.start('vm', test.id, 'Windows enhancement task', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('completed')
    const requests = test.client.requests.filter(request => request.method === 'guest_enhancement')
    expect(requests).toHaveLength(steps.length)
    for (const [index, request] of requests.entries()) {
      const step = steps[index]
      expect(request.claim).toBe('a'.repeat(48))
      expect(request.parameters).toEqual({ ...identity, operation: step.name, parameters: step.parameters, request_id: expect.stringMatching(/^[a-f0-9]{32}$/), ...(step.frame_id ? { frame_id: step.frame_id } : {}) })
      expect(request.parameters.parameters).not.toHaveProperty('frame_id')
    }
    expect(new Set(requests.map(request => request.parameters.request_id)).size).toBe(steps.length)
    const messages = test.requests[0].messages as { role: string; content: unknown }[]
    const instructions = JSON.stringify(messages.filter(message => message.role === 'system'))
    expect(instructions).toContain('You have no host filesystem or shell tools')
    expect(instructions).toContain('Windows enhancement tools')
    expect(instructions).not.toContain('Guest shell and edit tools are unavailable')
    expect(test.client.calls.at(-1)).toBe('driver_release')
  } finally { await test.close() }
}, 30000)

it('rejects invalid Windows tool arguments in the real SDK before sending any guest operation', async () => {
  const test = await environment()
  const identity = { instance_id: 'instance', boot_epoch: 1 }
  try {
    test.script([
      { name: 'powershell', arguments: { ...identity, command: 'Get-Process', timeout_seconds: 61 } },
      { name: 'multi_select', arguments: { ...identity, locs: [[10, 20]] } },
      { name: 'multi_edit', arguments: { ...identity, frame_id: 'frame', locs: [[10, 'missing-y']] } },
      { name: 'multi_edit', arguments: { ...identity, frame_id: 'frame', locs: [[10, 'invalid-y', 'text']] } },
      { name: 'multi_edit', arguments: { ...identity, frame_id: 'frame', labels: [['invalid-label', 'text']] } },
      { name: 'registry', arguments: { ...identity, mode: 'set', path: 'HKCU:\\Software\\Palette', name: 'Invalid', value: { command: 'not registry data' } } },
      { name: 'app', arguments: { ...identity, mode: 'resize', window_id: 'window-1', window_size: [0, 600] } },
      { name: 'snapshot', arguments: { ...identity, max_elements: 2001 } }
    ])
    await test.service.start('vm', test.id, 'invalid enhancement arguments', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('completed')
    expect(test.client.calls).not.toContain('guest_enhancement')
    expect(result.view.run?.stepCount).toBe(0)
    expect(test.client.calls.at(-1)).toBe('driver_release')
  } finally { await test.close() }
}, 20000)

it('stops after an unknown Windows batch without sending the next guest command', async () => {
  const test = await environment()
  const identity = { instance_id: 'instance', boot_epoch: 1 }
  try {
    test.client.unknownEnhancement = true
    test.script([
      { name: 'multi_edit', arguments: { ...identity, frame_id: 'frame', locs: [[10, 20, '提交']] } },
      { name: 'powershell', arguments: { ...identity, command: 'Write-Output next' } }
    ])
    await test.service.start('vm', test.id, 'Windows batch task', randomUUID())
    const result = await settled(test.service, test.id)
    expect(result.view.run?.state).toBe('cancelled')
    const requests = test.client.requests.filter(request => request.method === 'guest_enhancement')
    expect(requests).toHaveLength(1)
    expect(requests[0].parameters).toMatchObject({ operation: 'multi_edit', frame_id: 'frame' })
    expect(test.client.calls.at(-1)).toBe('driver_release')
  } finally { await test.close() }
}, 20000)
