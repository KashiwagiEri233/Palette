import { useCallback, useEffect, useRef, useState } from 'react'
import type { Conversation, ConversationPatch } from '../../shared/workspace'

export function useConversations(reportError: (error: unknown) => void) {
  const [records, setRecords] = useState<Conversation[]>([])
  const current = useRef<Conversation[]>([])
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const latestDrafts = useRef<Record<string, string>>({})
  const [draftStates, setDraftStates] = useState<Record<string, 'saving' | 'saved' | 'error'>>({})
  const pendingDrafts = useRef(new Map<string, string>())
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const queue = useRef<Promise<unknown>>(Promise.resolve())
  const pendingCreation = useRef<{ vmId: string; draft: string; requestId: string } | null>(null)
  const namespace = useRef<string | undefined>(undefined)
  const replace = useCallback((items: Conversation[]) => { current.current = items; setRecords(items) }, [])
  const load = useCallback(async (scope?: string) => {
    if (scope !== undefined && scope !== namespace.current) {
      namespace.current = scope
      replace([])
      setDrafts({})
      latestDrafts.current = {}
      pendingDrafts.current.clear()
      setDraftStates({})
      pendingCreation.current = null
    }
    const result = await window.palette.request('conversations.list', undefined, {})
    replace(result.conversations)
    if (!pendingDrafts.current.size) {
      setDrafts({})
      latestDrafts.current = {}
      setDraftStates({})
    }
  }, [replace])
  const update = useCallback((id: string, patch: ConversationPatch): Promise<Conversation> => {
    const result = queue.current.then(async () => {
      const item = current.current.find(item => item.id === id)
      if (!item) throw new Error('对话不存在')
      const updated = await window.palette.request('conversations.update', item.vm_id, { id, revision: item.revision, patch })
      replace(current.current.map(item => item.id === id ? updated : item))
      return updated
    })
    queue.current = result.catch(() => {})
    return result
  }, [replace])
  const flush = useCallback(async () => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
    const entries = [...pendingDrafts.current.entries()]
    pendingDrafts.current.clear()
    let failure: unknown
    for (const [id, draft] of entries) {
      try {
        await update(id, { draft })
        if (latestDrafts.current[id] === draft) setDraftStates(previous => ({ ...previous, [id]: 'saved' }))
      } catch (error) {
        if (latestDrafts.current[id] === draft) {
          if (!pendingDrafts.current.has(id)) pendingDrafts.current.set(id, draft)
          setDraftStates(previous => ({ ...previous, [id]: 'error' }))
        }
        failure ??= error
      }
    }
    await queue.current
    if (failure) throw failure
  }, [update])
  const setDraft = useCallback((id: string, value: string) => {
    setDrafts(previous => ({ ...previous, [id]: value }))
    latestDrafts.current[id] = value
    setDraftStates(previous => ({ ...previous, [id]: 'saving' }))
    pendingDrafts.current.set(id, value)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => { void flush().catch(reportError) }, 500)
  }, [flush, reportError])
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  const create = useCallback(async (vmId: string, draft = '') => {
    await flush()
    if (!pendingCreation.current || pendingCreation.current.vmId !== vmId || pendingCreation.current.draft !== draft) pendingCreation.current = { vmId, draft, requestId: crypto.randomUUID() }
    const item = await window.palette.request('conversations.create', vmId, { requestId: pendingCreation.current.requestId, draft })
    pendingCreation.current = null
    replace([...current.current.filter(previous => previous.id !== item.id), item])
    return item
  }, [flush, replace])
  const remove = useCallback(async (id: string) => {
    await flush()
    const item = current.current.find(item => item.id === id)
    if (!item) return
    await window.palette.request('conversations.delete', item.vm_id, { id, revision: item.revision })
    replace(current.current.filter(item => item.id !== id))
    setDrafts(previous => { const next = { ...previous }; delete next[id]; return next })
  }, [flush, replace])
  return { conversations: records.map(item => ({ ...item, draft: drafts[item.id] ?? item.draft })), draftStates, load, create, update, remove, setDraft, flush }
}
