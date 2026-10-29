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
  if (previous?.streamEpoch === next.streamEpoch && previous.seq > next.seq) return previous
  if (!previous || previous.view.conversationId !== next.view.conversationId) return next
  const cached = new Map(previous.view.messages.map(message => [message.id, message]))
  const messages = next.view.messages.map(message => {
    const old = cached.get(message.id)
    return old && JSON.stringify(old) === JSON.stringify(message) ? old : message
  })
  const same = messages.length === previous.view.messages.length && messages.every((message, index) => message === previous.view.messages[index])
  return { ...next, view: { ...next.view, messages: same ? previous.view.messages : messages } }
}

export function useAgent(vmId: string, conversationId: string, reportError: (error: unknown) => void) {
  const [snapshot, setSnapshot] = useState<AgentSnapshot | null>(null)
  const scope = useRef<string | null>(null)
  const requestScope = `${vmId}:${conversationId}`
  const refresh = useCallback(async () => {
    const value = await window.palette.request('agent.snapshot', vmId, { conversationId })
    if (scope.current === `${vmId}:${conversationId}`) setSnapshot(previous => acceptAgentSnapshot(previous, value))
    return value
  }, [vmId, conversationId])
  useEffect(() => {
    let disposed = false
    scope.current = requestScope
    setSnapshot(null)
    // Subscribe first; the snapshot and sequenced full views close the bootstrap race.
    const unsubscribe = window.palette.onAgentEvent(event => {
      if (!disposed && event.view.conversationId === conversationId) setSnapshot(previous => acceptAgentSnapshot(previous, event))
    })
    void refresh().catch(reportError)
    const focus = () => { void refresh().catch(reportError) }
    window.addEventListener('focus', focus)
    const timer = setInterval(focus, 5000)
    return () => { disposed = true; scope.current = null; unsubscribe(); clearInterval(timer); window.removeEventListener('focus', focus) }
  }, [conversationId, refresh, reportError])
  return { snapshot: snapshot?.view.conversationId === conversationId ? snapshot : null, refresh }
}
