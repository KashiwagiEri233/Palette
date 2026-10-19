export interface Conversation {
  id: string
  vm_id: string
  title: string
  draft: string
  pinned: boolean
  archived: boolean
  revision: number
  createdAt: string
  updatedAt: string
}

export type ConversationPatch = Partial<Pick<Conversation, 'title' | 'draft' | 'pinned' | 'archived'>>

export interface WorkspaceRoutes {
  'conversations.list': { payload: Record<string, never>; result: { conversations: Conversation[] } }
  'conversations.create': { payload: { requestId: string; draft?: string }; result: Conversation }
  'conversations.update': { payload: { id: string; revision: number; patch: ConversationPatch }; result: Conversation }
  'conversations.delete': { payload: { id: string; revision: number }; result: { deleted: true } }
  'conversations.export': { payload: { id: string }; result: { saved: boolean } }
  'window.command': { payload: { action: 'close' | 'menu' } | { action: 'open_external'; url: string }; result: { accepted: boolean } }
}

export type WorkspaceMethod = keyof WorkspaceRoutes
export const workspaceMethods = new Set<string>([
  'conversations.list', 'conversations.create', 'conversations.update',
  'conversations.delete', 'conversations.export', 'window.command'
])
