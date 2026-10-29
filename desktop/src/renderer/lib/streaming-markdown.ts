import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'

const parser = unified().use(remarkParse).use(remarkGfm)
export interface MarkdownSegment { start: number; text: string }

/** Keep two trailing top-level blocks mutable, including an unclosed fence/list/table.
 * Reference definitions can affect older blocks; completion always reparses the full document.
 */
export class StreamingMarkdownCache {
  private source = ''
  private offset = 0
  private stable: MarkdownSegment[] = []

  update(text: string, streaming: boolean): MarkdownSegment[] {
    if (!streaming) {
      this.source = ''; this.offset = 0; this.stable = []
      return [{ start: 0, text }]
    }
    if (!text.startsWith(this.source)) { this.offset = 0; this.stable = [] }
    this.source = text
    const tail = text.slice(this.offset)
    const tree = parser.parse(tail)
    const boundary = tree.children.length > 2 ? tree.children[tree.children.length - 2].position?.start.offset ?? 0 : 0
    if (boundary > 0) {
      this.stable.push({ start: this.offset, text: tail.slice(0, boundary) })
      this.offset += boundary
    }
    return [...this.stable, { start: this.offset, text: text.slice(this.offset) }]
  }
}
