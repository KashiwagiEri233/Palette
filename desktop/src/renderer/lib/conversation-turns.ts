import type { AgentContentBlock, AgentMessageView } from '../../shared/agent'

export interface ConversationTurn { id: string; user?: AgentMessageView; messages: AgentMessageView[] }
export interface TurnBlock { block: AgentContentBlock; streaming: boolean }

export function conversationTurns(messages: AgentMessageView[]): ConversationTurn[] {
  const turns: ConversationTurn[] = []
  for (const message of messages) {
    if (message.role === 'user') turns.push({ id: message.id, user: message, messages: [] })
    else {
      if (!turns.length) turns.push({ id: message.turnId ?? 'history', messages: [] })
      turns[turns.length - 1].messages.push(message)
    }
  }
  return turns
}

export function turnContent(turn: ConversationTurn): { process: TurnBlock[]; answer: TurnBlock[] } {
  const last = turn.messages.filter(message => message.role === 'assistant').at(-1)
  const final = last && !last.blocks?.some(block => block.type === 'tool-call') ? last : undefined
  const process: TurnBlock[] = []
  const answer: TurnBlock[] = []
  for (const message of turn.messages) {
    const blocks = message.blocks ?? (message.text ? [{ id: message.id, type: 'text' as const, text: message.text }] : [])
    for (const block of blocks) {
      const target = message === final && block.type !== 'reasoning' ? answer : process
      target.push({ block, streaming: message.status === 'streaming' })
    }
  }
  return { process, answer }
}
