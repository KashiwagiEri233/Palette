import { memo } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { externalLink } from '../../shared/external-link'

interface Props {
  text: string
  onError(error: unknown): void
}

const remarkPlugins = [remarkGfm]

export const MessageMarkdown = memo(function MessageMarkdown({ text, onError }: Props) {
  return <div className="agent-message-markdown">
    <Markdown skipHtml remarkPlugins={remarkPlugins} components={{
      a: ({ href, children }) => {
        const url = externalLink(href)
        return url ? <a href={url} title={url} onClick={event => {
          event.preventDefault()
          void window.palette.request('window.command', undefined, { action: 'open_external', url }).catch(onError)
        }}>{children}</a> : <span>{children}</span>
      },
      img: ({ alt }) => alt ? <span className="markdown-image-alt">{alt}</span> : null,
      table: ({ children }) => <div className="markdown-table" role="region" aria-label="表格" tabIndex={0}><table>{children}</table></div>
    }}>{text}</Markdown>
  </div>
})
