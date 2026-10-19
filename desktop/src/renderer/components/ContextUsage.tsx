import { useId } from 'react'
import type { AgentContextUsage } from '../../shared/agent'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

interface Props {
  usage: AgentContextUsage | null | undefined
  compacting: boolean
  compactDisabled: boolean
  onCompact(): void
}

const formatTokens = (tokens: number) => new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(tokens)

export function ContextUsage({ usage, compacting, compactDisabled, onCompact }: Props) {
  const titleId = useId()
  const percent = usage?.percent == null ? null : Math.min(100, Math.max(0, usage.percent))
  const used = percent === null ? null : Math.round(percent)
  const label = compacting ? '正在压缩上下文' : used === null ? '上下文使用状况：待更新' : `上下文使用状况：已用 ${used}%`
  return <Popover>
    <PopoverTrigger openOnHover delay={200} closeDelay={150} render={<Button variant="ghost" size="icon-sm" aria-label={label} className="context-usage-trigger" />}>
      <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true" className={compacting ? 'context-usage-compacting' : undefined}>
        <circle cx="10" cy="10" r="7" stroke="currentColor" opacity="0.25" strokeWidth="2.5" />
        <circle cx="10" cy="10" r="7" pathLength="100" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeDasharray={`${compacting ? 25 : percent ?? 0} 100`} transform="rotate(-90 10 10)" />
      </svg>
    </PopoverTrigger>
    <PopoverContent side="top" align="end" aria-labelledby={titleId} className="context-usage-popover">
      <p id={titleId} className="text-sm font-medium">上下文使用状况</p>
      <p className="mt-2 text-sm tabular-nums">{used === null ? '用量待更新' : `${used}% 已用（剩余 ${100 - used}%）`}</p>
      <p className="mt-1 text-xs text-muted-foreground tabular-nums">{usage ? usage.tokens === null ? `共 ${formatTokens(usage.contextWindow)} tokens，下一次回复后更新` : `已用 ${formatTokens(usage.tokens)} / ${formatTokens(usage.contextWindow)} tokens（估算）` : '配置模型后可查看用量'}</p>
      <Button variant="secondary" size="sm" className="mt-3 w-full" disabled={compactDisabled || compacting} onClick={onCompact}>{compacting ? '正在压缩…' : '压缩上下文'}</Button>
    </PopoverContent>
  </Popover>
}
