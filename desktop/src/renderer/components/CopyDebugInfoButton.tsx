import { useEffect, useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { Button } from '@/components/ui/button'
import type { CopyDebugInfoRequest } from '../../shared/diagnostics'

export function CopyDebugInfoButton(request: CopyDebugInfoRequest) {
  const [state, setState] = useState<'idle' | 'copying' | 'copied' | 'failed'>('idle')
  useEffect(() => { setState('idle') }, [request.error, request.vmId, request.conversationId, request.source])
  const copy = async () => {
    if (state === 'copying') return
    setState('copying')
    try { await window.palette.copyDebugInfo(request); setState('copied') }
    catch { setState('failed') }
  }
  return <Button variant="ghost" size="sm" className="copy-debug-info" aria-label="复制调试信息" disabled={state === 'copying'} onClick={() => void copy()}>
    {state === 'copied' ? <Check size={14} /> : <Copy size={14} />}
    <span aria-live="polite">{state === 'copied' ? '已复制' : state === 'failed' ? '复制失败，重试' : state === 'copying' ? '正在复制…' : '复制调试信息'}</span>
  </Button>
}

export function ErrorWithDebugInfo({ className = '', source = 'application', ...request }: Omit<CopyDebugInfoRequest, 'source'> & { className?: string; source?: CopyDebugInfoRequest['source'] }) {
  return <div role="alert" className={`error-with-debug-info ${className}`}><span>{request.error}</span><CopyDebugInfoButton source={source} {...request} /></div>
}
