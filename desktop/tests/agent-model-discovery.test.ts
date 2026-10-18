import { it, expect } from 'vitest'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { discoverAgentModels } from '../src/main/agent-model-discovery'

async function server(handler: (request: IncomingMessage, response: ServerResponse) => void) {
  const instance = createServer(handler)
  await new Promise<void>(resolve => instance.listen(0, '127.0.0.1', resolve))
  const baseUrl = `http://127.0.0.1:${(instance.address() as { port: number }).port}`
  return { baseUrl, close: async () => { instance.closeAllConnections(); await new Promise<void>(resolve => instance.close(() => resolve())) } }
}
const json = (response: ServerResponse, value: unknown) => { response.writeHead(200, { 'content-type': 'application/json' }); response.end(JSON.stringify(value)) }

it.each(['openai-completions', 'openai-responses', 'anthropic-messages'] as const)('discovers %s models with protocol authentication, pagination and deduplication', async api => {
  const paths: string[] = []
  const fixture = await server((request, response) => {
    paths.push(request.url!)
    if (api === 'anthropic-messages') { expect(request.headers['x-api-key']).toBe('test-secret'); expect(request.headers['anthropic-version']).toBe('2023-06-01'); expect(request.headers.authorization).toBeUndefined() }
    else expect(request.headers.authorization).toBe('Bearer test-secret')
    if (request.url!.includes('?')) json(response, { data: [{ id: 'shared', name: 'Do not replace' }, { id: 'second', display_name: 'Second', vision: true, reasoning: true, context_window: 64000, max_tokens: 4096 }], has_more: false })
    else json(response, { data: [{ id: 'shared', display_name: 'First' }], has_more: true, last_id: 'shared' })
  })
  try {
    const result = await discoverAgentModels({ api, baseUrl: api === 'anthropic-messages' ? fixture.baseUrl : `${fixture.baseUrl}/v1` }, 'test-secret')
    expect(paths).toEqual(['/v1/models', `/v1/models?${api === 'anthropic-messages' ? 'after_id' : 'after'}=shared`])
    expect(result.models).toEqual([{ id: 'shared', name: 'First', contextWindow: 128000, maxTokens: 8192, vision: false, reasoning: false }, { id: 'second', name: 'Second', contextWindow: 64000, maxTokens: 4096, vision: true, reasoning: true }])
  } finally { await fixture.close() }
})

it.each([[401, '认证失败'], [403, '认证失败'], [404, '不支持'], [405, '不支持'], [500, 'HTTP 500']] as const)('returns safe errors for HTTP %s without echoing credentials', async (status, message) => {
  const fixture = await server((_request, response) => { response.writeHead(status); response.end('test-secret echoed upstream') })
  try {
    const error = await discoverAgentModels({ api: 'openai-completions', baseUrl: fixture.baseUrl }, 'test-secret').catch(error => error as Error)
    expect(error).toBeInstanceOf(Error)
    expect((error as Error).message).toContain(message)
    expect((error as Error).message).not.toContain('test-secret')
  } finally { await fixture.close() }
})

it('rejects redirects before any credentials reach the target', async () => {
  let targetRequests = 0
  const target = await server((_request, response) => { targetRequests++; json(response, { data: [] }) })
  const fixture = await server((_request, response) => { response.writeHead(302, { location: target.baseUrl }); response.end() })
  try { await expect(discoverAgentModels({ api: 'openai-completions', baseUrl: fixture.baseUrl }, 'test-secret')).rejects.toThrow('重定向'); expect(targetRequests).toBe(0) }
  finally { await fixture.close(); await target.close() }
})

it('reads explicit Anthropic capability and token-limit metadata', async () => {
  const fixture = await server((_request, response) => json(response, { data: [{ id: 'claude-fixture', display_name: 'Claude fixture', max_input_tokens: 200000, max_tokens: 32000, capabilities: { image_input: { supported: true }, thinking: { supported: true } } }] }))
  try {
    expect((await discoverAgentModels({ api: 'anthropic-messages', baseUrl: fixture.baseUrl }, 'test-secret')).models).toEqual([{ id: 'claude-fixture', name: 'Claude fixture', contextWindow: 200000, maxTokens: 32000, vision: true, reasoning: true }])
  } finally { await fixture.close() }
})

it('times out a stalled response', async () => {
  const fixture = await server(() => {})
  try { await expect(discoverAgentModels({ api: 'openai-completions', baseUrl: fixture.baseUrl }, 'test-secret', 40)).rejects.toThrow('超时') }
  finally { await fixture.close() }
})

it.each([
  { value: { error: 'test-secret' }, message: '有效的模型列表' },
  { value: { data: [{ id: 'one' }], has_more: true, last_id: 'one' }, message: '分页无效' },
  { value: { data: Array.from({ length: 1001 }, (_, index) => ({ id: `model-${index}` })) }, message: '1000' },
  { value: { data: [{ id: 'bad\nmodel' }] }, message: '无效的模型参数' },
])('rejects malformed or oversized model collections', async ({ value, message }) => {
  const fixture = await server((_request, response) => json(response, value))
  try { await expect(discoverAgentModels({ api: 'openai-completions', baseUrl: fixture.baseUrl }, 'test-secret')).rejects.toThrow(message) }
  finally { await fixture.close() }
})

it('bounds response bytes and handles invalid JSON without returning raw server content', async () => {
  const fixture = await server((request, response) => { response.writeHead(200); response.end(request.url!.startsWith('/large') ? 'x'.repeat(1024 * 1024 + 1) : 'test-secret not json') })
  try {
    await expect(discoverAgentModels({ api: 'openai-completions', baseUrl: `${fixture.baseUrl}/large` }, 'test-secret')).rejects.toThrow('响应过大')
    await expect(discoverAgentModels({ api: 'openai-completions', baseUrl: fixture.baseUrl }, 'test-secret')).rejects.toThrow('有效的模型列表')
  } finally { await fixture.close() }
})
