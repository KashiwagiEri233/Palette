import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { MessageMarkdown } from '../src/renderer/components/MessageMarkdown'
import { externalLink } from '../src/shared/external-link'

const render = (text: string) => renderToStaticMarkup(createElement(MessageMarkdown, { text, onError: () => {} }))

describe('conversation Markdown', () => {
  it('renders structured text, code and GFM tables without exposing Markdown markers', () => {
    const html = render('# Result\n\n**Done** and *verified* with `status`.\n\n- First\n- Second\n\n1. Observe\n2. Act\n\n> Summary\n\n```ts\nconst value = "<safe>"\n```\n\n| Check | Result |\n| --- | --- |\n| Input | Passed |\n\n~~old~~\n\n- [x] Complete')
    for (const fragment of ['<h1>Result</h1>', '<strong>Done</strong>', '<em>verified</em>', '<code>status</code>', '<ul>', '<ol>', '<blockquote>', 'class="language-ts"', '&lt;safe&gt;', '<table>', '<th>Check</th>', '<td>Passed</td>', '<del>old</del>', 'type="checkbox" disabled="" checked=""']) {
      expect(html).toContain(fragment)
    }
    expect(html).not.toContain('**Done**')
  })

  it('does not render raw HTML, executable links or automatically fetched images', () => {
    const html = render('<script>alert(1)</script>\n\n<img src="https://example.com/tracker" onerror="alert(1)">\n\n[bad](javascript:alert%281%29)\n\n[local](file:///etc/passwd)\n\n![Diagram](https://example.com/diagram.png)\n\n[Docs](https://example.com/docs)')
    expect(html).not.toMatch(/<script|<img|onerror=|href="(?:javascript|file):/)
    expect(html).toContain('Diagram')
    expect(html).toContain('href="https://example.com/docs"')
  })

  it('renders unfinished streaming code blocks and updates them when complete', () => {
    expect(render('Reply\n\n```ts\nconst value = 1')).toContain('<pre><code class="language-ts">const value = 1')
    expect(render('Reply\n\n```ts\nconst value = 1\n```\n\n**Done**')).toContain('<strong>Done</strong>')
  })
})

describe('external Markdown links', () => {
  it('accepts absolute web URLs and rejects local or executable schemes and credentials', () => {
    expect(externalLink('https://example.com/docs?q=1#result')).toBe('https://example.com/docs?q=1#result')
    expect(externalLink('http://localhost:8080')).toBe('http://localhost:8080/')
    for (const value of [undefined, 1, '/etc/passwd', '//example.com', 'file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,hello', 'mailto:user@example.com', 'https://user:secret@example.com', 'https://example.com/' + 'a'.repeat(8192)]) {
      expect(externalLink(value)).toBeNull()
    }
  })
})
