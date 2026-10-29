import type { AgentMessage } from '@earendil-works/pi-agent-core'
import type { SessionEntry } from '@earendil-works/pi-coding-agent'
import type { AgentContentBlock, AgentImageReference, AgentMessageView, AgentRunView, AgentTodo, AgentToolView } from '../shared/agent'
import { activeRun } from '../shared/agent'
import { normalizeTodos, toolSummary } from '../shared/agent-content'

export const contentText = (content: unknown): string => typeof content === 'string' ? content
  : Array.isArray(content) ? content.filter(part => part?.type === 'text').map(part => part.text).join('\n') : ''

type ResultEntry = SessionEntry & { type: 'message'; message: Extract<AgentMessage, { role: 'toolResult' }> }

function images(entry: { id: string; message: { content: unknown } }): AgentImageReference[] {
  return Array.isArray(entry.message.content) ? entry.message.content.flatMap((part, contentIndex) =>
    part?.type === 'image' && part.mimeType === 'image/png'
      ? [{ entryId: entry.id, contentIndex, mimeType: 'image/png' as const }] : []) : []
}

/** Never project transport signatures or image bytes into the chat snapshot. */
function publicValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(publicValue)
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !/signature|base64|authorization|api.?key/i.test(key))
    .map(([key, item]) => [key, publicValue(item)]))
  return value
}

export function projectMessages(branch: SessionEntry[], streaming: AgentMessage | null | undefined,
  pending: ReadonlySet<string>, run: AgentRunView | null, partialArguments: ReadonlyMap<number, string> = new Map()): { messages: AgentMessageView[]; todos: AgentTodo[] } {
  const results = new Map<string, ResultEntry>()
  const called = new Set<string>()
  const todoHistory = new Map<string, { todos: AgentTodo[]; previous?: AgentTodo[] }>()
  let todos: AgentTodo[] = []
  let previous: AgentTodo[] | undefined
  for (const entry of branch) {
    if (entry.type !== 'message') continue
    if (entry.message.role === 'user') { todos = []; previous = undefined }
    if (entry.message.role === 'toolResult') {
      // IDs supplied by providers may be reused in a later user turn.
      if (entry.message.toolName === 'todo_write' && !entry.message.isError) {
        try {
          todos = normalizeTodos((entry.message.details as { todos?: unknown } | undefined)?.todos)
          todoHistory.set(entry.id, { todos, previous })
          previous = todos
        } catch { /* Old or invalid results never replace a valid plan. */ }
      }
    }
  }
  // Pair calls within their user turn, not globally across the transcript.
  let resultTurn = 'history'
  for (const entry of branch) {
    if (entry.type !== 'message') continue
    if (entry.message.role === 'user') resultTurn = entry.id
    if (entry.message.role === 'toolResult') results.set(`${resultTurn}:${entry.message.toolCallId}`, entry as ResultEntry)
  }
  const messages: AgentMessageView[] = []
  let turnId = 'history'
  let step = 0
  const lastUser = branch.filter(entry => entry.type === 'message' && entry.message.role === 'user').at(-1)?.id
  const append = (entryId: string | undefined, message: AgentMessage, live: boolean) => {
    if (message.role !== 'user' && message.role !== 'assistant' && message.role !== 'toolResult') return
    if (message.role === 'user') { turnId = entryId!; step = 0 }
    const id = message.role === 'assistant' ? `${turnId}:step:${step++}` : entryId!
    const blocks: AgentContentBlock[] = []
    const content = typeof message.content === 'string' ? [{ type: 'text' as const, text: message.content }] : message.content
    content.forEach((part, index) => {
      const blockId = `${id}:${index}`
      if (part.type === 'text') blocks.push({ id: blockId, type: 'text', text: part.text })
      else if (part.type === 'thinking') blocks.push({ id: blockId, type: 'reasoning', text: part.thinking })
      else if (part.type === 'toolCall') {
        const key = `${turnId}:${part.id}`
        called.add(key)
        const result = results.get(key)
        const detail = result?.message.details as Record<string, unknown> | undefined
        const error = detail?.error as { code?: string } | undefined
        const text = result ? contentText(result.message.content) : undefined
        const current = turnId === lastUser
        const stopped = current && !!run && ['stopping', 'cancelled', 'interrupted'].includes(run.state)
        const cancelled = !!result?.message.isError && /^(?:Operation aborted|The operation was aborted|This operation was aborted|任务已停止|已达到最大工具调用次数)/i.test(text ?? '')
        const unknown = detail?.effect_state === 'unknown' || error?.code === 'ACTION_OUTCOME_UNKNOWN'
          || current && run?.error?.includes('结果未知') && !result
        const status = unknown ? 'unknown' : result ? (result.message.isError ? (cancelled ? 'cancelled' : 'error') : 'complete')
          : live ? 'preparing' : pending.has(part.id) && current ? 'running'
          : current && activeRun(run) && !stopped ? 'preparing' : stopped ? 'cancelled' : 'ended'
        const history = result ? todoHistory.get(result.id) : undefined
        const tool: AgentToolView = {
          callId: part.id, name: part.name, arguments: publicValue(part.arguments), status,
          ...(live ? { partialArguments: partialArguments.get(index) ?? JSON.stringify(part.arguments, null, 2) } : {}),
          ...(result ? { result: publicValue(result.message.details), resultText: text } : {}),
          images: result ? images(result) : [],
          ...(result?.message.durationMs === undefined ? {} : { durationMs: result.message.durationMs }),
          ...(history ? { todos: history.todos, previousTodos: history.previous } : {})
        }
        blocks.push({ id: blockId, type: 'tool-call', tool })
      } else if (part.type === 'image') {
        if (entryId && part.mimeType === 'image/png') blocks.push({ id: blockId, type: 'image', image: { entryId, contentIndex: index, mimeType: 'image/png' } })
      } else blocks.push({ id: blockId, type: 'other', value: publicValue(part) })
    })
    if (message.role === 'toolResult') {
      // Matched results are rendered inside their call, preserving the original call order.
      if (called.has(`${turnId}:${message.toolCallId}`)) return
    }
    const text = contentText(message.content)
    messages.push({ id, ...(entryId ? { entryId } : {}), turnId, role: message.role === 'toolResult' ? 'tool' : message.role,
      text, blocks, ...(message.role === 'toolResult' ? { toolName: message.toolName } : {}),
      status: live ? 'streaming' : message.role === 'assistant' && ['error', 'aborted'].includes(message.stopReason) ? 'error' : 'complete' })
  }
  for (const entry of branch) {
    if (entry.type === 'message') append(entry.id, entry.message, false)
    else if (entry.type === 'compaction') messages.push({ id: entry.id, turnId, role: 'system', text: '上下文已压缩', status: 'complete' })
  }
  if (streaming?.role === 'assistant') append(undefined, streaming, true)
  return { messages, todos }
}

export function exportMessage(message: AgentMessageView): string {
  return message.blocks?.map(block => block.type === 'text' ? block.text : block.type === 'reasoning'
    ? `<details><summary>思考</summary>\n\n${block.text}\n\n</details>` : block.type === 'tool-call'
      ? `${toolSummary(block.tool)}\n\n\`\`\`json\n${JSON.stringify({ arguments: block.tool.arguments, result: block.tool.result ?? block.tool.resultText }, null, 2)}\n\`\`\``
      : block.type === 'image' ? '[截图：在 Palette 中查看]' : `\`\`\`json\n${JSON.stringify(block.value, null, 2)}\n\`\`\``).join('\n\n') || message.text
}

export function messageImage(branch: SessionEntry[], entryId: unknown, contentIndex: unknown): { mimeType: 'image/png'; dataBase64: string } {
  if (typeof entryId !== 'string' || !Number.isInteger(contentIndex) || (contentIndex as number) < 0) throw new Error('截图引用无效')
  const entry = branch.find(item => item.id === entryId)
  if (entry?.type !== 'message' || entry.message.role !== 'toolResult') throw new Error('截图不属于当前对话分支')
  const content = entry.message.content[contentIndex as number]
  if (content?.type !== 'image' || content.mimeType !== 'image/png' || typeof content.data !== 'string'
    || content.data.length > Math.ceil(8 * 1024 * 1024 / 3) * 4 || content.data.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(content.data)) throw new Error('截图格式或大小无效')
  const bytes = Buffer.from(content.data, 'base64')
  if (bytes.length > 8 * 1024 * 1024 || bytes.length < 24 || !bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
    || bytes.toString('base64') !== content.data) throw new Error('截图格式或大小无效')
  return { mimeType: 'image/png', dataBase64: content.data }
}
