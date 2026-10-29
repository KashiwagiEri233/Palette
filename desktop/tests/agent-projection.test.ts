import { describe, expect, it } from 'vitest'
import { SessionManager, type SessionEntry } from '@earendil-works/pi-coding-agent'
import type { AssistantMessage, ToolResultMessage } from '@earendil-works/pi-ai'
import { exportMessage, messageImage, projectMessages } from '../src/main/agent-projection'
import { normalizeTodos, todoChanges } from '../src/shared/agent-content'
import { conversationTurns, turnContent } from '../src/renderer/lib/conversation-turns'
import { acceptAgentSnapshot } from '../src/renderer/lib/use-agent'
import type { AgentRunView, AgentSnapshot } from '../src/shared/agent'

const run: AgentRunView = { id: 'run', state: 'running', stepCount: 1 }
const pixel = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP1sAAAAASUVORK5CYII='
const assistant = (content: AssistantMessage['content'], stopReason: AssistantMessage['stopReason'] = 'stop'): AssistantMessage => ({
  role: 'assistant', content, api: 'openai-completions', provider: 'fixture', model: 'fixture', stopReason, timestamp: 1,
  usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }
})
const call = (id = 'call', name = 'observe'): AssistantMessage['content'][number] => ({ type: 'toolCall', id, name, arguments: { count: 1 } })
const result = (toolName = 'observe', extras: Partial<ToolResultMessage> = {}): ToolResultMessage => ({
  role: 'toolResult', toolCallId: 'call', toolName, content: [{ type: 'text', text: 'result' }, { type: 'image', mimeType: 'image/png', data: pixel }],
  isError: false, timestamp: 2, durationMs: 15, details: { ok: true, data: { text: 'guest result' } }, ...extras
})
const session = () => {
  const manager = SessionManager.inMemory()
  const user = manager.appendMessage({ role: 'user', content: 'task', timestamp: 0 })
  return { manager, user }
}

describe('structured conversation projection', () => {
  it('preserves block order, partial arguments and stable identity across settlement', () => {
    const { manager } = session()
    const message = assistant([{ type: 'thinking', thinking: 'Plan', thinkingSignature: 'opaque' }, { type: 'text', text: 'Observe first' }, call()], 'toolUse')
    const live = projectMessages(manager.getBranch(), message, new Set(), run, new Map([[2, '{"count":']]))
    expect(live.messages[1].blocks?.map(block => block.type)).toEqual(['reasoning', 'text', 'tool-call'])
    const liveTool = live.messages[1].blocks?.[2]
    expect(liveTool?.type === 'tool-call' && liveTool.tool).toMatchObject({ status: 'preparing', partialArguments: '{"count":' })
    manager.appendMessage(message)
    const running = projectMessages(manager.getBranch(), null, new Set(['call']), run)
    expect(running.messages[1].id).toBe(live.messages[1].id)
    expect(running.messages[1].blocks?.[2]).toMatchObject({ type: 'tool-call', tool: { status: 'running' } })
    const entryId = manager.appendMessage(result())
    manager.appendMessage(assistant([{ type: 'text', text: 'Done' }]))
    const complete = projectMessages(manager.getBranch(), null, new Set(), { ...run, state: 'completed' })
    expect(complete.messages).toHaveLength(3)
    expect(complete.messages[1].blocks?.[2]).toMatchObject({ tool: { status: 'complete', result: { data: { text: 'guest result' } }, durationMs: 15, images: [{ entryId, contentIndex: 1 }] } })
    expect(JSON.stringify(complete)).not.toContain(pixel)
    expect(JSON.stringify(complete)).not.toContain('opaque')
    const grouped = turnContent(conversationTurns(complete.messages)[0])
    expect(grouped.process.map(item => item.block.type)).toEqual(['reasoning', 'text', 'tool-call'])
    expect(grouped.answer[0].block).toMatchObject({ text: 'Done' })
    expect(exportMessage(complete.messages[1])).toContain('guest result')
  })

  it('keeps pure thinking, partial output, failed and uncertain tool outcomes', () => {
    const { manager } = session()
    manager.appendMessage(assistant([{ type: 'thinking', thinking: 'Partial thought' }], 'aborted'))
    manager.appendMessage(assistant([call()], 'toolUse'))
    const stopped = projectMessages(manager.getBranch(), null, new Set(), { ...run, state: 'cancelled' })
    expect(stopped.messages[1]).toMatchObject({ status: 'error', blocks: [{ text: 'Partial thought' }] })
    expect(stopped.messages[2].blocks?.[0]).toMatchObject({ tool: { status: 'cancelled' } })
    expect(projectMessages(manager.getBranch(), null, new Set(), null).messages[2].blocks?.[0]).toMatchObject({ tool: { status: 'ended' } })
    manager.appendMessage(result('observe', { isError: true, details: { effect_state: 'unknown' } }))
    expect(projectMessages(manager.getBranch(), null, new Set(), run).messages[2].blocks?.[0]).toMatchObject({ tool: { status: 'unknown' } })
  })

  it('does not turn earlier tool errors into cancellation when the user stops later', () => {
    const { manager } = session()
    manager.appendMessage(assistant([call()], 'toolUse'))
    manager.appendMessage(result('observe', { isError: true, content: [{ type: 'text', text: 'Guest operation failed' }] }))
    expect(projectMessages(manager.getBranch(), null, new Set(), { ...run, state: 'cancelled' }).messages[1].blocks?.[0]).toMatchObject({ tool: { status: 'error' } })
  })

  it('does not pair reused tool IDs from separate user turns and renders legacy messages', () => {
    const { manager } = session()
    manager.appendMessage(assistant([call()], 'toolUse'))
    manager.appendMessage(result())
    manager.appendMessage({ role: 'user', content: 'next task', timestamp: 3 })
    manager.appendMessage(assistant([call()], 'toolUse'))
    const projected = projectMessages(manager.getBranch(), null, new Set(['call']), run)
    expect(projected.messages[1].blocks?.[0]).toMatchObject({ tool: { status: 'complete' } })
    expect(projected.messages[3].blocks?.[0]).toMatchObject({ tool: { status: 'running', images: [] } })
    const legacy = conversationTurns([{ id: 'old', role: 'assistant', text: 'Legacy', status: 'complete' }])
    expect(turnContent(legacy[0]).answer[0].block).toMatchObject({ type: 'text', text: 'Legacy' })
  })
})

describe('task snapshots', () => {
  it('normalizes full lists and rejects invalid or parallel states', () => {
    expect(normalizeTodos([{ content: ' Task ', status: 'pending' }])).toEqual([{ content: 'Task', status: 'pending' }])
    for (const invalid of [[{ content: ' ', status: 'pending' }], [{ content: 'x', status: 'invalid' }], [{ content: 'x', status: 'pending' }, { content: ' x ', status: 'completed' }], [{ content: 'x', status: 'in_progress' }, { content: 'y', status: 'in_progress' }]]) expect(() => normalizeTodos(invalid)).toThrow()
    expect(normalizeTodos([])).toEqual([])
  })

  it('uses the last successful result, compares history and clears at the next user turn', () => {
    const { manager } = session()
    const first = [{ content: 'Read', status: 'in_progress' as const }, { content: 'Write', status: 'pending' as const }]
    const next = [{ content: 'Write', status: 'completed' as const }, { content: 'Review', status: 'pending' as const }]
    for (const [todos, isError] of [[first, false], [next, false], [[], true]] as const) {
      manager.appendMessage(assistant([call(`call-${manager.getBranch().length}`, 'todo_write')], 'toolUse'))
      manager.appendMessage(result('todo_write', { toolCallId: `call-${manager.getBranch().length - 1}`, content: [], details: { todos }, isError }))
    }
    expect(projectMessages(manager.getBranch(), null, new Set(), run).todos).toEqual(next)
    const changes = todoChanges(first, next)
    expect(changes.join(' ')).toContain('新增：Review')
    expect(changes.join(' ')).toContain('删除：Read')
    expect(changes.join(' ')).toContain('待开始 → 已完成')
    expect(changes.join(' ')).toContain('顺序变化：Write')
    const branch = manager.getBranch()
    manager.appendMessage({ role: 'user', content: 'next', timestamp: 5 })
    expect(projectMessages(manager.getBranch(), null, new Set(), run).todos).toEqual([])
    expect(projectMessages(branch, null, new Set(), null).todos).toEqual(next)
  })
})

describe('on-demand images', () => {
  it('reads only valid PNG blocks on the current branch', () => {
    const { manager, user } = session()
    const entry = manager.appendMessage(result())
    expect(messageImage(manager.getBranch(), entry, 1)).toEqual({ mimeType: 'image/png', dataBase64: pixel })
    for (const [id, index] of [[user, 1], [entry, -1], [entry, 0], [entry, 1.1], ['other', 1], [entry, '1']]) expect(() => messageImage(manager.getBranch(), id, index)).toThrow()
    expect(() => messageImage(manager.getBranch().slice(0, 1), entry, 1)).toThrow('分支')
  })

  it('rejects corrupt, non-PNG and oversized data', () => {
    const { manager } = session()
    for (const [data, mimeType] of [['!!!!', 'image/png'], [Buffer.from('invalid header').toString('base64'), 'image/png'], [pixel, 'image/jpeg'], ['A'.repeat(Math.ceil(8 * 1024 * 1024 / 3) * 4 + 4), 'image/png']]) {
      const id = manager.appendMessage(result('observe', { content: [{ type: 'image', data, mimeType }] }))
      expect(() => messageImage(manager.getBranch(), id, 0)).toThrow()
    }
  })
})

it('ignores older snapshots and preserves unchanged historical message identity', () => {
  const { manager } = session()
  const messages = projectMessages(manager.getBranch(), null, new Set(), null).messages
  const snapshot: AgentSnapshot = { streamEpoch: 'epoch', seq: 2, view: { conversationId: 'conversation', run: null, messages, tree: [], compacting: false, contextUsage: null } }
  expect(acceptAgentSnapshot(snapshot, { ...snapshot, seq: 1 })).toBe(snapshot)
  const next = acceptAgentSnapshot(snapshot, JSON.parse(JSON.stringify({ ...snapshot, seq: 3 })))
  expect(next.view.messages).toBe(messages)
  expect(acceptAgentSnapshot(snapshot, { ...snapshot, streamEpoch: 'new', seq: 0 }).streamEpoch).toBe('new')
})
