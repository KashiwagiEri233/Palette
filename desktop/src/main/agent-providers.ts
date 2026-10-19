import { randomUUID } from 'node:crypto'
import { join } from 'node:path'
import type { AgentApiKind, AgentConfiguration, AgentModelDefinition, CustomAgentProvider } from '../shared/agent'
import { atomicJson, readJson } from './agent-storage'

export const agentApis = new Set<AgentApiKind>(['openai-completions', 'openai-responses', 'anthropic-messages', 'google-generative-ai'])
export const customAgentApis = new Set(['openai-completions', 'openai-responses', 'anthropic-messages'])
export const defaultConfiguration: AgentConfiguration = { providerId: 'openai', modelId: '', api: 'openai-responses', baseUrl: '', custom: false, contextWindow: 128000, maxTokens: 8192, vision: false, reasoning: false, thinkingLevel: 'medium', maxSteps: 100 }
export const validProviderId = (value: unknown): value is string => typeof value === 'string' && /^[\w.-]{1,128}$/.test(value) && value !== '.' && value !== '..'
const validText = (value: unknown, max: number): value is string => typeof value === 'string' && !!value.trim() && value.length <= max && !/[\x00-\x1f\x7f]/.test(value)

export function validateBaseUrl(value: unknown): string {
  if (typeof value !== 'string' || value.length > 2048) throw new Error('模型地址无效')
  let url: URL
  try { url = new URL(value) } catch { throw new Error('模型地址无效') }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.hash || url.search) throw new Error('模型地址必须是不含凭据和查询参数的 HTTP(S) 地址')
  return url.toString().replace(/\/$/, '')
}

export function validateModel(value: unknown): AgentModelDefinition {
  if (!value || typeof value !== 'object') throw new Error('模型配置无效')
  const item = value as AgentModelDefinition
  if (!validText(item.id, 256) || !validText(item.name, 256)
    || !Number.isInteger(item.contextWindow) || item.contextWindow < 1024 || item.contextWindow > 10_000_000
    || !Number.isInteger(item.maxTokens) || item.maxTokens < 1 || item.maxTokens > item.contextWindow
    || typeof item.vision !== 'boolean' || typeof item.reasoning !== 'boolean') throw new Error('模型配置无效，请检查 ID、名称、上下文与最大输出')
  return { id: item.id.trim(), name: item.name.trim(), contextWindow: item.contextWindow, maxTokens: item.maxTokens, vision: item.vision, reasoning: item.reasoning }
}

export function validateProvider(value: unknown, allowNew = false): CustomAgentProvider {
  if (!value || typeof value !== 'object') throw new Error('提供商配置无效')
  const item = value as CustomAgentProvider
  const id = allowNew && item.id === '' ? `custom-${randomUUID()}` : item.id
  if (!validProviderId(id) || !(id === 'palette-custom' || id.startsWith('custom-')) || !validText(item.name, 128)
    || !customAgentApis.has(item.api) || !Array.isArray(item.models) || item.models.length > 1000) throw new Error('提供商配置无效')
  const models = item.models.map(validateModel)
  if (new Set(models.map(model => model.id)).size !== models.length) throw new Error('同一提供商内的模型 ID 不能重复')
  return { id, name: item.name.trim(), api: item.api, baseUrl: validateBaseUrl(item.baseUrl), models }
}

export function validateKey(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !value.trim() || value.length > 8192 || /[\r\n\0]/.test(value)) throw new Error('API Key 无效')
}

export function validateAgentConfiguration(value: unknown): AgentConfiguration {
  if (!value || typeof value !== 'object') throw new Error('Agent 设置无效')
  const item = value as AgentConfiguration
  if (!validProviderId(item.providerId) || typeof item.modelId !== 'string' || item.modelId.length > 256 || !agentApis.has(item.api)
    || typeof item.baseUrl !== 'string' || typeof item.custom !== 'boolean'
    || !Number.isInteger(item.maxSteps) || item.maxSteps < 1 || item.maxSteps > 1000
    || !['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].includes(item.thinkingLevel)) throw new Error('Agent 设置无效')
  if (!item.modelId.trim()) return { ...defaultConfiguration, thinkingLevel: item.thinkingLevel, maxSteps: item.maxSteps }
  const model = validateModel({ ...item, id: item.modelId, name: item.modelId })
  return { providerId: item.providerId, modelId: model.id, api: item.api, baseUrl: item.custom || item.baseUrl ? validateBaseUrl(item.baseUrl) : '', custom: item.custom, contextWindow: model.contextWindow, maxTokens: model.maxTokens, vision: model.vision, reasoning: model.reasoning, thinkingLevel: item.thinkingLevel, maxSteps: item.maxSteps }
}

export interface AgentProviderState {
  version: 2
  configuration: AgentConfiguration
  providers: CustomAgentProvider[]
  builtinProviderIds: string[]
}

export class AgentProviderStore {
  private pending?: Promise<AgentProviderState>
  private current?: AgentProviderState
  constructor(private readonly directory: string) {}
  load(): Promise<AgentProviderState> {
    if (this.current) return Promise.resolve(this.current)
    return this.pending ??= this.initialize().catch(error => { this.pending = undefined; throw error })
  }
  private async initialize(): Promise<AgentProviderState> {
    const saved = await readJson<AgentProviderState | null>(join(this.directory, 'providers.json'), null)
    if (saved) {
      if (saved.version !== 2 || !Array.isArray(saved.providers)) throw new Error('提供商配置版本无效')
      const providers = saved.providers.map(provider => validateProvider(provider))
      if (new Set(providers.map(provider => provider.id)).size !== providers.length) throw new Error('提供商 ID 重复')
      if (saved.builtinProviderIds !== undefined && (!Array.isArray(saved.builtinProviderIds) || !saved.builtinProviderIds.every(validProviderId))) throw new Error('提供商配置无效')
      return this.current = { version: 2, configuration: validateAgentConfiguration(saved.configuration), providers, builtinProviderIds: saved.builtinProviderIds ?? [] }
    }
    const legacy = await readJson<AgentConfiguration | null>(join(this.directory, 'settings.json'), null)
    const configuration = legacy ? validateAgentConfiguration(legacy) : { ...defaultConfiguration }
    const providers: CustomAgentProvider[] = []
    if (configuration.custom && configuration.modelId) {
      providers.push(validateProvider({ id: 'palette-custom', name: '自定义端点', api: configuration.api, baseUrl: configuration.baseUrl, models: [{ ...configuration, id: configuration.modelId, name: configuration.modelId }] }))
      configuration.providerId = 'palette-custom'
    }
    const state: AgentProviderState = { version: 2, configuration, providers, builtinProviderIds: !configuration.custom && configuration.modelId ? [configuration.providerId] : [] }
    // The legacy file and its encrypted credential remain untouched for recovery.
    if (legacy) await atomicJson(join(this.directory, 'providers.json'), state)
    return this.current = state
  }
  async save(state: AgentProviderState): Promise<void> {
    await atomicJson(join(this.directory, 'providers.json'), state)
    this.current = state
  }
}
