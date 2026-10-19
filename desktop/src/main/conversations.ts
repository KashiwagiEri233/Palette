import { createHash, randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import type { Conversation, ConversationPatch } from '../shared/workspace'

const validId = (id: unknown): id is string => typeof id === 'string' && /^[\w.-]{1,128}$/.test(id)
const validText = (text: unknown, limit: number): text is string => typeof text === 'string' && text.length <= limit
const validRequestId = (id: unknown): id is string => typeof id === 'string' && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id)

function isConversation(value: unknown): value is Conversation {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const item = value as Record<string, unknown>
  return validId(item.id) && validId(item.vm_id) && validText(item.title, 120) && validText(item.draft, 16_000)
    && typeof item.pinned === 'boolean' && typeof item.archived === 'boolean'
    && typeof item.revision === 'number' && Number.isInteger(item.revision) && item.revision >= 1
    && typeof item.createdAt === 'string' && Number.isFinite(Date.parse(item.createdAt))
    && typeof item.updatedAt === 'string' && Number.isFinite(Date.parse(item.updatedAt))
}

/** Serialized atomic persistence. A separate store is used for each configuration root. */
export class ConversationRepository {
  private queue: Promise<unknown> = Promise.resolve()
  private readonly file: string

  constructor(userData: string, configRoot: string) {
    const identity = createHash('sha256').update(configRoot).digest('hex')
    this.file = join(userData, 'workspaces', identity, 'conversations.json')
  }

  private async read(): Promise<{ conversations: Conversation[]; requests: Record<string, string> }> {
    let source: string
    try { source = await readFile(this.file, 'utf8') }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return { conversations: [], requests: {} }; throw error }
    const damaged = () => new Error('对话记录无法读取，请检查存储目录')
    let decoded: unknown
    try { decoded = JSON.parse(source) } catch { throw damaged() }
    if (!decoded || typeof decoded !== 'object' || Array.isArray(decoded)) throw damaged()
    const data = decoded as Record<string, unknown>
    if (data.version !== 1 || !Array.isArray(data.conversations) || !data.conversations.every(isConversation) || !data.requests || typeof data.requests !== 'object' || Array.isArray(data.requests)) throw damaged()
    if (!Object.entries(data.requests).every(([key, value]) => validRequestId(key) && validId(value))) throw damaged()
    return { conversations: data.conversations, requests: data.requests as Record<string, string> }
  }

  private serialize<T>(work: () => Promise<T>): Promise<T> {
    const result = this.queue.then(work)
    this.queue = result.catch(() => {})
    return result
  }

  private async write(data: { conversations: Conversation[]; requests: Record<string, string> }): Promise<void> {
    await mkdir(dirname(this.file), { recursive: true })
    const temporary = `${this.file}.${randomUUID()}.tmp`
    try {
      await writeFile(temporary, JSON.stringify({ ...data, version: 1 }), { mode: 0o600 })
      await rename(temporary, this.file)
    } catch (error) { await unlink(temporary).catch(() => {}); throw error }
  }

  list(): Promise<Conversation[]> {
    return this.serialize(async () => (await this.read()).conversations)
  }

  create(vmId: string, requestId: unknown, draft: unknown = ''): Promise<Conversation> {
    return this.serialize(async () => {
      if (!validId(vmId) || !validRequestId(requestId) || !validText(draft, 16_000)) throw new Error('会话参数无效')
      const data = await this.read()
      const previous = data.conversations.find(item => item.id === data.requests[requestId])
      if (previous) {
        if (previous.vm_id !== vmId) throw new Error('新对话请求与虚拟机不匹配')
        return previous
      }
      const now = new Date().toISOString()
      const conversation: Conversation = { id: randomUUID(), vm_id: vmId, title: draft.trim().slice(0, 40) || '新对话', draft, pinned: false, archived: false, revision: 1, createdAt: now, updatedAt: now }
      data.conversations.push(conversation)
      data.requests[requestId] = conversation.id
      await this.write(data)
      return conversation
    })
  }

  update(vmId: string, id: unknown, revision: unknown, patch: unknown): Promise<Conversation> {
    return this.serialize(async () => {
      const data = await this.read()
      const item = data.conversations.find(item => item.id === id && item.vm_id === vmId)
      if (!item) throw new Error('对话不存在或不属于此虚拟机')
      if (revision !== item.revision) throw new Error('对话已更新，请刷新后重试')
      if (!patch || typeof patch !== 'object' || Array.isArray(patch)) throw new Error('对话参数无效')
      const values = patch as ConversationPatch
      if (Object.keys(values).some(key => !['title', 'draft', 'pinned', 'archived'].includes(key)) || (values.title !== undefined && (!validText(values.title, 120) || !values.title.trim())) || (values.draft !== undefined && !validText(values.draft, 16_000)) || (values.pinned !== undefined && typeof values.pinned !== 'boolean') || (values.archived !== undefined && typeof values.archived !== 'boolean')) throw new Error('对话参数无效')
      Object.assign(item, values, { revision: item.revision + 1, updatedAt: new Date().toISOString() })
      if (values.title !== undefined) item.title = values.title.trim()
      await this.write(data)
      return item
    })
  }

  remove(vmId: string, id: unknown, revision: unknown): Promise<void> {
    return this.serialize(async () => {
      const data = await this.read()
      const item = data.conversations.find(item => item.id === id && item.vm_id === vmId)
      if (!item) throw new Error('对话不存在或不属于此虚拟机')
      if (revision !== item.revision) throw new Error('对话已更新，请刷新后重试')
      data.conversations = data.conversations.filter(candidate => candidate.id !== id)
      data.requests = Object.fromEntries(Object.entries(data.requests).filter(([, value]) => value !== id))
      await this.write(data)
    })
  }

  removeVm(vmId: string): Promise<void> {
    return this.serialize(async () => {
      const data = await this.read()
      data.conversations = data.conversations.filter(item => item.vm_id !== vmId)
      const remaining = new Set(data.conversations.map(item => item.id))
      data.requests = Object.fromEntries(Object.entries(data.requests).filter(([, value]) => remaining.has(value)))
      await this.write(data)
    })
  }
}
