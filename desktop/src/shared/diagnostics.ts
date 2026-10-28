export interface CopyDebugInfoRequest {
  source: 'agent' | 'application' | 'provider'
  error: string
  vmId?: string
  conversationId?: string
}
