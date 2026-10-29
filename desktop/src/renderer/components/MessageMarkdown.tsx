import { Children, isValidElement, memo, useMemo, useRef } from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { externalLink } from '../../shared/external-link'
import { MessageCodeBlock } from './MessageCodeBlock'
import { StreamingMarkdownCache } from '../lib/streaming-markdown'

interface Props {
  text: string
  streaming?: boolean
  onError(error: unknown): void
}

const remarkPlugins = [remarkGfm]

const MarkdownChunk = memo(function MarkdownChunk({ text, onError }: Props) {
  const errorHandler = useRef(onError)
  errorHandler.current = onError
  // Stable renderer identities preserve code-copy state across status polling.
  const components = useMemo<Components>(() => ({
      pre: ({ children }) => {
        const child = Children.toArray(children)[0]
        if (!isValidElement<{ className?: string; children?: unknown }>(child)) return <pre>{children}</pre>
        const language = /(?:^|\s)language-([^\s]+)/.exec(child.props.className ?? '')?.[1]
        const code = String(child.props.children ?? '')
        return <MessageCodeBlock code={code} language={language} />
      },
      a: ({ href, children }) => {
        const url = externalLink(href)
        return url ? <a href={url} title={url} onClick={event => {
          event.preventDefault()
          void window.palette.request('window.command', undefined, { action: 'open_external', url }).catch(error => errorHandler.current(error))
        }}>{children}</a> : <span>{children}</span>
      },
      img: ({ alt }) => alt ? <span className="markdown-image-alt">{alt}</span> : null,
      table: ({ children }) => <div className="markdown-table" role="region" aria-label="表格" tabIndex={0}><table>{children}</table></div>
    }), [])
  return <Markdown skipHtml remarkPlugins={remarkPlugins} components={components}>{text}</Markdown>
})

export const MessageMarkdown = memo(function MessageMarkdown({ text, streaming = false, onError }: Props) {
  const cache = useRef(new StreamingMarkdownCache())
  const chunks = cache.current.update(text, streaming)
  return <div className="agent-message-markdown">{chunks.map(chunk => <MarkdownChunk key={chunk.start} text={chunk.text} onError={onError} />)}</div>
})
