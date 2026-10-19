import type { AgentModelDefinition, AgentModelDiscovery } from '../shared/agent'
import { customAgentApis, validateBaseUrl, validateKey, validateModel } from './agent-providers'

class DiscoveryError extends Error {}

async function readResponse(response: Response): Promise<unknown> {
  if (!response.body) throw new DiscoveryError('模型列表响应为空')
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let size = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > 1024 * 1024) throw new DiscoveryError('模型列表响应过大，请手动添加模型')
      chunks.push(value)
    }
    try { return JSON.parse(Buffer.concat(chunks).toString('utf8')) }
    catch { throw new DiscoveryError('服务未返回有效的模型列表') }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
}

export async function discoverAgentModels(input: AgentModelDiscovery, key: string, timeoutMs = 15_000): Promise<{ models: AgentModelDefinition[] }> {
  if (!input || !customAgentApis.has(input.api)) throw new Error('不支持此 API 的模型发现')
  const baseUrl = validateBaseUrl(input.baseUrl)
  validateKey(key)
  const url = new URL(`${baseUrl}${input.api === 'anthropic-messages' ? '/v1/models' : '/models'}`)
  const headers: Record<string, string> = input.api === 'anthropic-messages'
    ? { 'x-api-key': key, 'anthropic-version': '2023-06-01' }
    : { authorization: `Bearer ${key}` }
  const signal = AbortSignal.timeout(timeoutMs)
  const models = new Map<string, AgentModelDefinition>()
  const cursors = new Set<string>()
  try {
    for (let page = 0; page < 100; page++) {
      const response = await fetch(url, { headers, signal, redirect: 'manual' })
      if (response.status >= 300 && response.status < 400) { await response.body?.cancel(); throw new DiscoveryError('模型列表接口发生重定向，请填写服务的最终 Base URL') }
      if (!response.ok) {
        await response.body?.cancel()
        throw new DiscoveryError(response.status === 401 || response.status === 403 ? '认证失败，请检查 API Key 和服务权限' : response.status === 404 || response.status === 405 ? '服务不支持获取模型列表，请手动添加模型' : `获取模型失败（HTTP ${response.status}），请稍后重试或手动添加`)
      }
      const payload = await readResponse(response) as { data?: unknown; has_more?: unknown; last_id?: unknown }
      if (!payload || !Array.isArray(payload.data)) throw new DiscoveryError('服务未返回有效的模型列表，请手动添加模型')
      for (const item of payload.data) {
        if (!item || typeof item !== 'object') throw new DiscoveryError('模型列表格式无效')
        const entry = item as Record<string, unknown>
        if (typeof entry.id !== 'string') throw new DiscoveryError('模型列表缺少模型 ID')
        const capabilities = entry.capabilities as { image_input?: { supported?: unknown }; thinking?: { supported?: unknown }; effort?: { supported?: unknown } } | null | undefined
        let model: AgentModelDefinition
        try {
          model = validateModel({ id: entry.id, name: entry.display_name ?? entry.name ?? entry.id,
            contextWindow: entry.context_window ?? entry.max_input_tokens ?? 128000, maxTokens: entry.max_tokens ?? 8192,
            vision: typeof entry.vision === 'boolean' ? entry.vision : capabilities?.image_input?.supported === true,
            reasoning: typeof entry.reasoning === 'boolean' ? entry.reasoning : capabilities?.thinking?.supported === true || capabilities?.effort?.supported === true })
        } catch { throw new DiscoveryError('模型列表包含无效的模型参数，请手动添加模型') }
        if (!models.has(model.id)) models.set(model.id, model)
        if (models.size > 1000) throw new DiscoveryError('模型列表超过 1000 个，请手动添加所需模型')
      }
      if (payload.has_more !== true) return { models: [...models.values()] }
      const cursor = payload.last_id ?? (payload.data.at(-1) as { id?: unknown } | undefined)?.id
      if (typeof cursor !== 'string' || !cursor || cursors.has(cursor)) throw new DiscoveryError('模型列表分页无效，请手动添加模型')
      cursors.add(cursor)
      url.searchParams.set(input.api === 'anthropic-messages' ? 'after_id' : 'after', cursor)
    }
    throw new DiscoveryError('模型列表分页过多，请手动添加模型')
  } catch (error) {
    if (signal.aborted) throw new Error('获取模型超时，请重试或手动添加模型')
    if (error instanceof DiscoveryError) throw error
    // Do not forward upstream response bodies, URLs or network errors containing secrets.
    throw new Error('无法获取模型列表，请检查地址与网络，或手动添加模型')
  }
}
