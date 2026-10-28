/** Bounded error evidence kept in main until the user explicitly copies a report. */
export function scrubDebugInfo(value: unknown, depth = 0, secret?: string): unknown {
  if (depth > 8) return '[depth limit]'
  if (typeof value === 'string') return (secret ? value.split(secret).join('[redacted]') : value)
    .replace(/https?:\/\/[^\s"<>]+/g, raw => {
      try { const url = new URL(raw); url.username = ''; url.password = ''; url.search = ''; url.hash = ''; return url.toString() }
      catch { return '[endpoint]' }
    })
    .replace(/(Bearer|Basic)\s+[^\s"<>]+/gi, '$1 [redacted]')
    .replace(/((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)["']?\s*[=:]\s*["']?)[^\s"',;&]+/gi, '$1[redacted]')
    .slice(0, 16000)
  if (Array.isArray(value)) return value.slice(-32).map(item => scrubDebugInfo(item, depth + 1, secret))
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).slice(0, 64)
    .filter(([key]) => !/^(?:api[_-]?key|authorization|headers|cookie|password|secret|token|access_token|refresh_token|claim_token|lease_id|messages|prompt|content|text|body|payload|parameters|arguments|argv)$/i.test(key))
    .map(([key, item]) => [key, scrubDebugInfo(item, depth + 1, secret)]))
  return typeof value === 'bigint' ? value.toString() : value
}

function messageKey(value: string): string {
  return value.replace(/^(?:Error: )?Error invoking remote method '[^']+':\s*/, '').replace(/^(?:Error|BackendError):\s*/, '')
}

export class DebugInfoStore {
  private errors: { root: string; vmId?: string; operation: string; at: string; key: string; error: unknown }[] = []
  capture(root: string, operation: string, error: unknown, vmId?: string, secret?: unknown): void {
    const item = error instanceof Error ? error : new Error(String(error))
    const extra = item as Error & { code?: string; details?: unknown; target?: unknown }
    const evidence = scrubDebugInfo({ name: item.name, message: item.message, stack: item.stack, code: extra.code, details: extra.details ?? extra.target }, 0, typeof secret === 'string' ? secret : undefined)
    this.errors.push({ root, vmId, operation, at: new Date().toISOString(), key: messageKey(item.message), error: evidence })
    if (this.errors.length > 20) this.errors.shift()
  }
  find(root: string, message: string, vmId?: string): unknown {
    const key = messageKey(message)
    const match = this.errors.slice().reverse().find(item => item.root === root && (!vmId || item.vmId === vmId) && (key === item.key || key.includes(item.key)))
    if (!match) return undefined
    return { at: match.at, operation: match.operation, vmId: match.vmId, error: match.error }
  }
}
