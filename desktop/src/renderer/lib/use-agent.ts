import { useCallback, useEffect, useRef, useState } from 'react'
import type { AgentSettings, AgentSnapshot } from '../../shared/agent'

export function useAgentSettings(scope?: string) {
  const [settings, setSettings] = useState<AgentSettings | null>(null)
  const reload = useCallback(async () => {
    const result = await window.palette.request('agent.settings.get', undefined, {})
    setSettings(result)
    return result
  }, [])
  useEffect(() => {
    let alive = true
    setSettings(null)
    const refresh = () => { void window.palette.request('agent.settings.get', undefined, {}).then(value => { if (alive) setSettings(value) }).catch(() => {}) }
    refresh()
    window.addEventListener('palette-agent-settings', refresh)
    return () => { alive = false; window.removeEventListener('palette-agent-settings', refresh) }
  }, [scope])
  return { settings, reload }
}

export function acceptAgentSnapshot(previous: AgentSnapshot | null, next: AgentSnapshot): AgentSnapshot {
  return previous?.streamEpoch === next.streamEpoch && previous.seq > next.seq ? previous : next
}

export function useAgent(vmId: string, conversationId: string, reportError: (error: unknown) => void) {
  const [snapshot, setSnapshot] = useState<AgentSnapshot | null>(null)
  const alive = useRef(false)
  const refresh = useCallback(async () => {
    const value = await window.palette.request('agent.snapshot', vmId, { conversationId })
    if (alive.current) setSnapshot(previous => acceptAgentSnapshot(previous, value))
    return value
  }, [vmId, conversationId])
  useEffect(() => {
    let disposed = false
    alive.current = true
    setSnapshot(null)
    // Subscribe first; the snapshot and sequenced full views close the bootstrap race.
    const unsubscribe = window.palette.onAgentEvent(event => {
      if (!disposed && event.view.conversationId === conversationId) setSnapshot(previous => acceptAgentSnapshot(previous, event))
    })
    void refresh().catch(reportError)
    const focus = () => { void refresh().catch(reportError) }
    window.addEventListener('focus', focus)
    const timer = setInterval(focus, 5000)
    return () => { disposed = true; alive.current = false; unsubscribe(); clearInterval(timer); window.removeEventListener('focus', focus) }
  }, [conversationId, refresh, reportError])
  return { snapshot, refresh }
}
