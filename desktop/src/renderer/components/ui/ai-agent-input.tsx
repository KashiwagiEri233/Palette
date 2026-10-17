import type { ReactNode } from 'react'
import { ArrowUp, Square } from '@/components/icons/workbench'
import { Button } from './button'
import { Textarea } from './textarea'
import { Card, CardFooter } from './card'

interface AgentInputProps {
  value: string
  onValueChange(value: string): void
  onSend(): void
  onStop?(): void
  running?: boolean
  disabled?: boolean
  sendDisabled?: boolean
  placeholder?: string
  sendLabel?: string
  footer?: ReactNode
  actions?: ReactNode
}

/** Model selection and execution are provided by the owner. */
export function PromptInput({ value, onValueChange, onSend, onStop, running = false, disabled = false, sendDisabled = false, placeholder = '让 Agent 操作这台虚拟机…', sendLabel = '发送', footer, actions }: AgentInputProps) {
  return <Card variant="input" className="agent-composer-card">
    <Textarea variant="composer" aria-label="任务内容" value={value} maxLength={16000} disabled={disabled} placeholder={placeholder} onChange={event => onValueChange(event.target.value)} onKeyDown={event => {
      if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
        event.preventDefault()
        if (!running && !disabled && !sendDisabled && value.trim()) onSend()
      }
    }} />
    <CardFooter className="agent-composer-controls"><div className="agent-composer-options">{footer}</div>
      {actions && <div className="agent-composer-actions">{actions}</div>}
      <Button size="icon-sm" aria-label={running ? '停止 Agent' : sendLabel} disabled={!running && (disabled || sendDisabled || !value.trim())} onClick={running ? onStop : onSend}>{running ? <Square size={16} /> : <ArrowUp size={16} />}</Button>
    </CardFooter>
  </Card>
}

export { PromptInput as AiAgentInput }
