import { memo, useEffect, useMemo, useRef, useState } from 'react'
import type {} from 'highlight.js'
import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import powershell from 'highlight.js/lib/languages/powershell'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'
import yaml from 'highlight.js/lib/languages/yaml'
import sql from 'highlight.js/lib/languages/sql'
import diff from 'highlight.js/lib/languages/diff'
import { Check, Copy } from 'lucide-react'

for (const [name, grammar] of Object.entries({ javascript, typescript, python, json, bash, powershell, xml, css, yaml, sql, diff })) hljs.registerLanguage(name, grammar)

export const MessageCodeBlock = memo(function MessageCodeBlock({ code, language = '' }: { code: string; language?: string }) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(timer.current), [])
  useEffect(() => { setCopyState('idle'); clearTimeout(timer.current) }, [code])
  const highlighted = useMemo(() => {
    if (!language || !hljs.getLanguage(language) || code.length > 100000) return null
    try { return hljs.highlight(code, { language, ignoreIllegals: true }).value } catch { return null }
  }, [code, language])
  const copy = async () => {
    try { await window.palette.request('window.command', undefined, { action: 'copy_text', text: code }); setCopyState('copied') }
    catch { setCopyState('failed') }
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopyState('idle'), 2000)
  }
  return <div className="message-code-block">
    <div className="message-code-header"><span>{language || '纯文本'}</span><button type="button" onClick={() => void copy()} aria-label="复制代码">
      {copyState === 'copied' ? <Check size={13} /> : <Copy size={13} />}<span aria-live="polite">{copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败，重试' : '复制'}</span>
    </button></div>
    <pre>{highlighted === null ? <code className={language ? `language-${language}` : undefined}>{code}</code>
      // Only highlight.js-generated, escaped markup enters this sink. Markdown HTML stays disabled.
      : <code className={`hljs language-${language}`} dangerouslySetInnerHTML={{ __html: highlighted }} />}</pre>
  </div>
})
