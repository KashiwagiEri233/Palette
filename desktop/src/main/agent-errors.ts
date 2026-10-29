/** Receives credential-redacted text; preserve the cause without blaming configuration. */
export function agentFailureMessage(detail: string, fallback = '模型请求失败'): string {
  detail = detail.replace(/https?:\/\/[^\s"<>]+/g, value => {
    try { const url = new URL(value); url.search = ''; url.hash = ''; url.username = ''; url.password = ''; return url.toString() }
    catch { return '[endpoint]' }
  }).replace(/Bearer\s+[^\s"<>]+/gi, 'Bearer [redacted]').trim().slice(0, 1200)
  const status = /^(\d{3})(?=[:\s])/.exec(detail)?.[1]
  let summary = fallback
  if (/invalid schema|invalid.*(?:tool|function).*schema/i.test(detail)) summary = '工具参数格式不兼容，模型请求被拒绝'
  else if (status === '401' || status === '403') summary = '模型请求鉴权失败'
  else if (status === '429') summary = '模型请求受到限流'
  return `${summary}${status ? `（HTTP ${status}）` : ''}${detail ? `：${detail.replace(/^\d{3}:\s*/, '')}` : '，未返回具体错误信息'}`
}
