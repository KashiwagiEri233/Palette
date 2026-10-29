import { memo, useEffect, useState, type ReactNode } from 'react'
import { Brain, Check, ChevronDown, Circle, CircleAlert, ListChecks, LoaderCircle, Square, Wrench } from 'lucide-react'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import type { AgentImageReference, AgentMessageView, AgentRunView, AgentTodo, AgentToolView } from '../../shared/agent'
import { activeRun } from '../../shared/agent'
import { todoChanges, toolStatusLabels, toolSummary } from '../../shared/agent-content'
import { conversationUiState } from '../lib/conversation-state'
import { conversationTurns, turnContent, type ConversationTurn, type TurnBlock } from '../lib/conversation-turns'
import { MessageMarkdown } from './MessageMarkdown'

function ProcessDisclosure({ scope, id, label, children, className = '' }: { scope: string; id: string; label: ReactNode; children: ReactNode; className?: string }) {
  const [open, setOpen] = useState(() => conversationUiState(scope).expanded.get(id) ?? false)
  return <Collapsible className={`process-disclosure ${className}`} open={open} onOpenChange={value => {
    conversationUiState(scope).expanded.set(id, value)
    setOpen(value)
  }}>
    <CollapsibleTrigger className="process-trigger"><ChevronDown size={14} className="process-chevron" />{label}</CollapsibleTrigger>
    <CollapsibleContent>{open && <div className="process-details">{children}</div>}</CollapsibleContent>
  </Collapsible>
}

function StatusIcon({ status }: { status: AgentToolView['status'] | AgentTodo['status'] }) {
  if (['running', 'preparing', 'in_progress'].includes(status)) return <LoaderCircle size={14} className="process-spinner" aria-hidden="true" />
  if (['complete', 'completed'].includes(status)) return <Check size={14} aria-hidden="true" />
  if (['error', 'unknown'].includes(status)) return <CircleAlert size={14} aria-hidden="true" />
  if (status === 'cancelled') return <Square size={12} aria-hidden="true" />
  return <Circle size={13} aria-hidden="true" />
}

export function TodoItems({ todos }: { todos: AgentTodo[] }) {
  const labels = { pending: '待开始', in_progress: '进行中', completed: '已完成' }
  return <ul className="agent-todo-list">{todos.map(item => <li key={item.content} data-todo-status={item.status}>
    <StatusIcon status={item.status} /><span>{item.content}</span><span className="sr-only">{labels[item.status]}</span>
  </li>)}</ul>
}

export function TodoPanel({ scope, todos }: { scope: string; todos: AgentTodo[] }) {
  if (!todos.length) return null
  const counts = [
    ['已完成', todos.filter(item => item.status === 'completed').length],
    ['进行中', todos.filter(item => item.status === 'in_progress').length],
    ['待开始', todos.filter(item => item.status === 'pending').length]
  ].filter(([, count]) => count)
  return <ProcessDisclosure scope={scope} id="todo-panel" className="agent-todo-panel" label={<><ListChecks size={15} /><span>任务进度</span><span className="process-summary">{counts.map(([label, count]) => `${label} ${count}`).join(' · ')}</span></>}>
    <TodoItems todos={todos} />
  </ProcessDisclosure>
}

function LazyScreenshot({ vmId, conversationId, image, number }: { vmId: string; conversationId: string; image: AgentImageReference; number: number }) {
  const [source, setSource] = useState<string | null>(null)
  const [error, setError] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [large, setLarge] = useState(false)
  useEffect(() => {
    let disposed = false
    let objectUrl: string | undefined
    setSource(null); setError(false)
    void window.palette.request('agent.message.image', vmId, { conversationId, entryId: image.entryId, contentIndex: image.contentIndex }).then(result => {
      if (disposed) return
      const bytes = Uint8Array.from(atob(result.dataBase64), char => char.charCodeAt(0))
      objectUrl = URL.createObjectURL(new Blob([bytes], { type: result.mimeType }))
      setSource(objectUrl)
    }).catch(() => { if (!disposed) setError(true) })
    return () => { disposed = true; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [vmId, conversationId, image.entryId, image.contentIndex, attempt])
  return <div className="tool-screenshot">
    {error ? <div role="status">截图加载失败 <Button variant="ghost" size="sm" onClick={() => setAttempt(value => value + 1)}>重试加载截图</Button></div>
      : source ? <button type="button" className="screenshot-preview" aria-label={`放大截图 ${number}`} onClick={() => setLarge(true)}><img src={source} alt={`工具截图 ${number}`} onError={() => setError(true)} /></button>
      : <span role="status">正在加载截图…</span>}
    <Dialog open={large} onOpenChange={setLarge}><DialogContent className="screenshot-dialog"><DialogTitle>工具截图 {number}</DialogTitle>{source && <img src={source} alt={`工具截图 ${number}，放大预览`} />}</DialogContent></Dialog>
  </div>
}

const ToolRow = memo(function ToolRow({ tool, scope, id, vmId, conversationId }: { tool: AgentToolView; scope: string; id: string; vmId: string; conversationId: string }) {
  const changes = tool.todos && tool.previousTodos ? todoChanges(tool.previousTodos, tool.todos) : undefined
  return <ProcessDisclosure scope={scope} id={id} className={`agent-tool-row tool-status-${tool.status}`} label={<>
    <StatusIcon status={tool.status} /><span>{toolSummary(tool)}</span>
    {tool.name === 'todo_write' && <span className="sr-only">更新任务进度 · {toolStatusLabels[tool.status]}</span>}
    {tool.durationMs !== undefined && <span className="tool-duration">{tool.durationMs < 1000 ? `${Math.round(tool.durationMs)} ms` : `${(tool.durationMs / 1000).toFixed(1)} s`}</span>}
  </>}>
    <span className="tool-detail-label">参数{tool.status === 'preparing' ? '（生成中）' : ''}</span>
    <pre className="tool-json">{tool.partialArguments ?? JSON.stringify(tool.arguments, null, 2)}</pre>
    {tool.todos ? <><TodoItems todos={tool.todos} />{changes ? <div className="todo-changes"><span className="tool-detail-label">进度变化</span>{changes.length ? <ul>{changes.map(change => <li key={change}>{change}</li>)}</ul> : <p>任务列表未变化</p>}</div> : <p className="field-hint">首次任务列表</p>}</>
      : (tool.result !== undefined || tool.resultText) && <><span className="tool-detail-label">结果 · {toolStatusLabels[tool.status]}</span><pre className="tool-json">{tool.result !== undefined ? JSON.stringify(tool.result, null, 2) : tool.resultText}</pre></>}
    {tool.images.map((image, index) => <LazyScreenshot key={`${image.entryId}:${image.contentIndex}`} vmId={vmId} conversationId={conversationId} image={image} number={index + 1} />)}
  </ProcessDisclosure>
})

function BlockView({ item, scope, vmId, conversationId, onError }: { item: TurnBlock; scope: string; vmId: string; conversationId: string; onError(error: unknown): void }) {
  const { block, streaming } = item
  if (block.type === 'text') return <MessageMarkdown text={block.text} streaming={streaming} onError={onError} />
  if (block.type === 'reasoning') return <ProcessDisclosure scope={scope} id={block.id} className="agent-reasoning" label={<><Brain size={14} /><span>思考</span><span className="process-summary">{block.text.split(/\n\s*\n/).filter(Boolean).at(-1)?.replace(/\s+/g, ' ').slice(0, 100)}</span></>}><MessageMarkdown text={block.text} streaming={streaming} onError={onError} /></ProcessDisclosure>
  if (block.type === 'tool-call') return <ToolRow scope={scope} id={block.id} tool={block.tool} vmId={vmId} conversationId={conversationId} />
  if (block.type === 'image') return <LazyScreenshot vmId={vmId} conversationId={conversationId} image={block.image} number={1} />
  return <pre className="tool-json">{JSON.stringify(block.value, null, 2)}</pre>
}

const TurnView = memo(function TurnView({ turn, run, scope, vmId, conversationId, onError }: { turn: ConversationTurn; run?: AgentRunView | null; scope: string; vmId: string; conversationId: string; onError(error: unknown): void }) {
  const { process, answer } = turnContent(turn)
  const tools = process.flatMap(item => item.block.type === 'tool-call' ? [item.block.tool] : [])
  const busy = activeRun(run)
  const current = tools.slice().reverse().find(tool => ['running', 'preparing'].includes(tool.status))
  const summary = busy ? current ? toolSummary(current) : process.some(item => item.block.type === 'reasoning') && !answer.length ? '正在思考…' : answer.length ? '正在回复…' : '正在等待回复…'
    : `${run ? ({ completed: '已完成', failed: '执行失败', cancelled: '已停止', interrupted: '任务已中断', starting: '正在启动', running: '正在执行', stopping: '正在停止' })[run.state] : '已结束'}${tools.length ? ` · ${tools.length} 次工具调用` : ''}`
  return <section className="agent-turn" data-turn-id={turn.id}>
    {turn.user && <article className="agent-message agent-message-user" data-message-role="user"><span className="agent-message-label">你</span><MessageMarkdown text={turn.user.text} onError={onError} /></article>}
    <article className="agent-message agent-message-assistant" data-message-role="assistant"><span className="agent-message-label">Palette</span>
      {(process.length > 0 || busy) && <ProcessDisclosure scope={scope} id={`${turn.id}:process`} className="agent-process" label={<>
        {busy ? <LoaderCircle size={14} className="process-spinner" /> : <Wrench size={14} />}<span>执行过程</span><span className="process-summary">{summary}</span>
      </>}>{process.length ? process.map(item => <BlockView key={item.block.id} item={item} scope={scope} vmId={vmId} conversationId={conversationId} onError={onError} />) : <p className="field-hint">正在等待模型输出</p>}</ProcessDisclosure>}
      <div className="agent-answer">{answer.map(item => <BlockView key={item.block.id} item={item} scope={scope} vmId={vmId} conversationId={conversationId} onError={onError} />)}</div>
      {turn.messages.some(message => message.status === 'error') && <span className="field-hint">回复未完成</span>}
    </article>
  </section>
})

export function ConversationTimeline({ messages, run, ...props }: { messages: AgentMessageView[]; run?: AgentRunView | null; scope: string; vmId: string; conversationId: string; onError(error: unknown): void }) {
  const turns = conversationTurns(messages)
  return <div className="agent-timeline" role="log" aria-label="对话消息" aria-live="off">{turns.map((turn, index) => <TurnView key={turn.id} turn={turn} run={index === turns.length - 1 ? run : undefined} {...props} />)}</div>
}
