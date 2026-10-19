export type AgentApiKind = 'openai-completions' | 'openai-responses' | 'anthropic-messages' | 'google-generative-ai'
export type AgentEffort = 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'
export type CustomAgentApiKind = Exclude<AgentApiKind, 'google-generative-ai'>
export interface AgentModelDefinition {
  id: string
  name: string
  contextWindow: number
  maxTokens: number
  vision: boolean
  reasoning: boolean
}
export interface CustomAgentProvider {
  id: string
  name: string
  api: CustomAgentApiKind
  baseUrl: string
  models: AgentModelDefinition[]
}
export interface AgentProviderView {
  id: string
  name: string
  custom: boolean
  credentialConfigured: boolean
  managed: boolean
  modelCount: number
  api?: CustomAgentApiKind
  baseUrl?: string
}
export interface AgentModel extends AgentModelDefinition {
  providerId: string
  api: AgentApiKind
  baseUrl: string
}
export interface AgentModelDiscovery {
  providerId?: string
  api: CustomAgentApiKind
  baseUrl: string
  key?: string
}
export interface AgentConfiguration {
  providerId: string
  modelId: string
  api: AgentApiKind
  baseUrl: string
  custom: boolean
  contextWindow: number
  maxTokens: number
  vision: boolean
  reasoning: boolean
  thinkingLevel: AgentEffort
  maxSteps: number
}
export interface AgentSettings {
  configuration: AgentConfiguration
  credentialConfigured: boolean
  credentialPersistent: boolean
  providers: AgentProviderView[]
  models: AgentModel[]
  guestProgramming: { available: false; reason: string }
}
export interface AgentMessageView {
  id: string
  role: 'user' | 'assistant' | 'tool' | 'system'
  text: string
  toolName?: string
  status: 'complete' | 'streaming' | 'error'
}
export interface AgentRunView {
  id: string
  state: 'starting' | 'running' | 'stopping' | 'completed' | 'cancelled' | 'failed' | 'interrupted'
  stepCount: number
  error?: string
}
export interface AgentTreeEntry { id: string; parentId: string | null; label: string; active: boolean }
export interface AgentContextUsage {
  tokens: number | null
  contextWindow: number
  percent: number | null
}
export interface AgentView {
  conversationId: string
  run: AgentRunView | null
  messages: AgentMessageView[]
  tree: AgentTreeEntry[]
  compacting: boolean
  contextUsage: AgentContextUsage | null
}
export interface AgentSnapshot { streamEpoch: string; seq: number; view: AgentView }
export interface AgentEvent extends AgentSnapshot { runId: string | null }
export interface AgentRoutes {
  'agent.settings.get': { payload: Record<string, never>; result: AgentSettings }
  'agent.settings.update': { payload: { configuration: AgentConfiguration }; result: AgentSettings }
  'agent.providers.save': { payload: { provider: CustomAgentProvider; key?: string }; result: AgentSettings }
  'agent.providers.delete': { payload: { providerId: string }; result: AgentSettings }
  'agent.models.discover': { payload: AgentModelDiscovery; result: { models: AgentModelDefinition[] } }
  'agent.credentials.set': { payload: { providerId: string; key: string }; result: { configured: boolean; persistent: boolean } }
  'agent.credentials.remove': { payload: { providerId: string }; result: { configured: false } }
  'agent.snapshot': { payload: { conversationId: string }; result: AgentSnapshot }
  'agent.start': { payload: { conversationId: string; text: string; requestId: string }; result: { runId: string } }
  'agent.stop': { payload: { conversationId: string }; result: AgentSnapshot }
  'agent.compact': { payload: { conversationId: string }; result: AgentSnapshot }
  'agent.tree.navigate': { payload: { conversationId: string; entryId: string }; result: AgentSnapshot & { editorText?: string } }
}
export type AgentMethod = keyof AgentRoutes
export const agentMethods = new Set<string>(['agent.settings.get', 'agent.settings.update', 'agent.providers.save', 'agent.providers.delete', 'agent.models.discover', 'agent.credentials.set', 'agent.credentials.remove', 'agent.snapshot', 'agent.start', 'agent.stop', 'agent.compact', 'agent.tree.navigate'])
export const activeRun = (run: AgentRunView | null | undefined): boolean => !!run && ['starting', 'running', 'stopping'].includes(run.state)
