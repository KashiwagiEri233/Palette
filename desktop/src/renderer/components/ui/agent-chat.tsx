import type { HTMLAttributes, ReactNode } from 'react'
import { cn } from '@/lib/utils'

interface AgentChatProps extends HTMLAttributes<HTMLElement> {
  header?: ReactNode
  composer?: ReactNode
}

/** Controlled chat layout; the visual primitive owns no messages or execution. */
export function AgentChat({ header, composer, children, className, ...props }: AgentChatProps) {
  return <section className={cn('agent-chat-panel', className)} {...props}>
    {header}
    <div className="agent-chat-messages">{children}</div>
    {composer}
  </section>
}
