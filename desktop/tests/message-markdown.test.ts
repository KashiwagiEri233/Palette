import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { MessageMarkdown } from '../src/renderer/components/MessageMarkdown'
import { externalLink } from '../src/shared/external-link'
import { StreamingMarkdownCache } from '../src/renderer/lib/streaming-markdown'

const render = (text: string) => renderToStaticMarkup(createElement(MessageMarkdown, { text, onError: () => {} }))

describe('conversation Markdown', () => {
  it('renders structured text, code and GFM tables without exposing Markdown markers', () => {
    const html = render('# Result\n\n**Done** and *verified* with `status`.\n\n- First\n- Second\n\n1. Observe\n2. Act\n\n> Summary\n\n```ts\nconst value = "<safe>"\n```\n\n| Check | Result |\n| --- | --- |\n| Input | Passed |\n\n~~old~~\n\n- [x] Complete')
    for (const fragment of ['<h1>Result</h1>', '<strong>Done</strong>', '<em>verified</em>', '<code>status</code>', '<ul>', '<ol>', '<blockquote>', 'language-ts', 'hljs-keyword', 'aria-label="复制代码"', '&lt;safe&gt;', '<table>', '<th>Check</th>', '<td>Passed</td>', '<del>old</del>', 'type="checkbox" disabled="" checked=""']) {
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
    expect(render('Reply\n\n```ts\nconst value = 1')).toContain('<span class="hljs-keyword">const</span>')
    expect(render('Reply\n\n```ts\nconst value = 1\n```\n\n**Done**')).toContain('<strong>Done</strong>')
  })

  it('falls back for unknown languages and escapes code containing HTML', () => {
    const html = render('```not-a-language\n<script>alert("x")</script>\n```')
    expect(html).toContain('class="language-not-a-language"')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
  })
})

describe('streaming Markdown cache', () => {
  it('reuses stable prefixes while fences, lists and tables grow, then reparses the full result', () => {
    const cache = new StreamingMarkdownCache()
    const first = '# Title\n\nIntro\n\n- first\n\n```ts\nconst value = 1'
    const chunks = cache.update(first, true)
    expect(chunks.map(chunk => chunk.text).join('')).toBe(first)
    const complete = first + '\n```\n\n| A | B |\n| - | - |\n| 1 | 2 |'
    const next = cache.update(complete, true)
    expect(next[0]).toBe(chunks[0])
    expect(next.map(chunk => chunk.text).join('')).toBe(complete)
    expect(cache.update(complete, false)).toEqual([{ start: 0, text: complete }])
  })

  it('resets for replacement text and resolves distant reference definitions on completion', () => {
    const cache = new StreamingMarkdownCache()
    const text = '[docs][ref]\n\n' + Array.from({ length: 200 }, (_, index) => `Paragraph ${index}.\n\n`).join('') + '[ref]: https://example.com'
    for (let end = 100; end < text.length; end += 100) cache.update(text.slice(0, end), true)
    const final = cache.update(text, false)
    expect(render(final[0].text)).toContain('href="https://example.com/"')
    expect(cache.update('Replacement **answer**', true)).toEqual([{ start: 0, text: 'Replacement **answer**' }])
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
