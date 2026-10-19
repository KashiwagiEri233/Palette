import type { HTMLAttributes, ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { Card } from './card'

interface AgentChatProps extends HTMLAttributes<HTMLDivElement> {
  header?: ReactNode
  composer?: ReactNode
}

/** Controlled chat layout; the visual primitive owns no messages or execution. */
export function AgentChat({ header, composer, children, className, ...props }: AgentChatProps) {
  return <Card variant="pane" data-slot="agent-chat" className={cn('agent-chat-panel', className)} {...props}>
    {header}
    <div className="agent-chat-messages">{children}</div>
    {composer}
  </Card>
}
