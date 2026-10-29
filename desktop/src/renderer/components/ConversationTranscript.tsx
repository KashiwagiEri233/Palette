import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { ArrowDown } from 'lucide-react'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Button } from '@/components/ui/button'
import type { AgentView } from '../../shared/agent'
import { activeRun } from '../../shared/agent'
import { toolLabels, toolStatusLabels } from '../../shared/agent-content'
import { conversationUiState } from '../lib/conversation-state'
import { ConversationTimeline } from './ConversationTimeline'
import { CopyDebugInfoButton } from './CopyDebugInfoButton'

export function ConversationTranscript({ scope, vmId, conversationId, view, empty, onError }: { scope: string; vmId: string; conversationId: string; view?: AgentView; empty: ReactNode; onError(error: unknown): void }) {
  const viewport = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const state = useRef(conversationUiState(scope))
  const restored = useRef(false)
  const previousMessages = useRef<AgentView['messages'] | undefined>(undefined)
  const [hasNew, setHasNew] = useState(false)
  const ready = !!view
  useLayoutEffect(() => {
    const element = viewport.current
    if (!element || !ready) return
    element.scrollTop = state.current.following ? element.scrollHeight : state.current.scrollTop
    restored.current = true
    const observer = new ResizeObserver(() => {
      if (state.current.following) element.scrollTop = element.scrollHeight
    })
    if (content.current) observer.observe(content.current)
    observer.observe(element)
    return () => {
      state.current.scrollTop = element.scrollTop
      observer.disconnect()
    }
  }, [ready])
  useLayoutEffect(() => {
    if (!restored.current || !view) return
    const previous = previousMessages.current
    previousMessages.current = view.messages
    if (!previous || previous === view.messages) return
    const element = viewport.current!
    if (state.current.following) element.scrollTop = element.scrollHeight
    else setHasNew(true)
  }, [view?.messages])
  const runText = view?.compacting ? '正在压缩上下文…' : view?.run ? `${({ starting: '正在启动', running: '正在执行', stopping: '正在停止', completed: '已完成', cancelled: '已停止', failed: '执行失败', interrupted: '任务已中断' })[view.run.state]} · ${view.run.stepCount} 次工具调用` : ''
  const latest = view?.messages.at(-1)
  const tool = latest?.blocks?.slice().reverse().find(block => block.type === 'tool-call')
  const phase = tool?.type === 'tool-call' ? `${toolLabels[tool.tool.name] ?? tool.tool.name}，${toolStatusLabels[tool.tool.status]}`
    : latest?.status === 'streaming' ? latest.blocks?.some(block => block.type === 'text' && block.text) ? '正在回复' : '正在思考' : ''
  return <div className="conversation-transcript">
    <ScrollArea className="conversation-scroll" viewportRef={viewport} onViewportScroll={() => {
      if (!restored.current || !viewport.current) return
      const element = viewport.current
      state.current.scrollTop = element.scrollTop
      state.current.following = element.scrollHeight - element.scrollTop - element.clientHeight <= 80
      if (state.current.following) setHasNew(false)
    }}><div ref={content}>
      {view?.messages.length ? <ConversationTimeline scope={scope} vmId={vmId} conversationId={conversationId} messages={view.messages} run={view.run} onError={onError} /> : empty}
      <div className="agent-run-status" role="status" aria-atomic="true">{runText}{activeRun(view?.run) && phase && <span className="sr-only"> · {phase}</span>}</div>
      {view?.run?.error && <div className="agent-run-error"><p>{view.run.error}</p><CopyDebugInfoButton source="agent" error={view.run.error} vmId={vmId} conversationId={conversationId} /></div>}
    </div></ScrollArea>
    {hasNew && <Button className="new-messages-button" variant="secondary" size="sm" onClick={() => {
      state.current.following = true
      if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight
      setHasNew(false)
    }}><ArrowDown size={14} />有新消息 ↓</Button>}
  </div>
}
