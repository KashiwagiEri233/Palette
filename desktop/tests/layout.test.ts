import { describe, expect, it } from 'vitest'
import { defaultLayout, parseLayout, resolveLayout } from '../src/renderer/lib/layout'

describe('workspace layout', () => {
  it('restores safe defaults for malformed, obsolete or nonfinite preferences', () => {
    for (const source of [null, 'undefined', 'null', '[]', '{"version":2}', '{"version":1,"chatPx":"900"}']) expect(parseLayout(source)).toEqual(defaultLayout)
    expect(parseLayout('{"version":1,"sidebarPx":999,"railPx":1,"chatPx":999,"sidebarHidden":"yes"}')).toEqual({ ...defaultLayout, sidebarPx: 280, railPx: 48, chatPx: 640 })
  })

  it('enforces the exact 1280 and 1100 breakpoints without overwriting preferences', () => {
    expect(resolveLayout(1280, defaultLayout)).toMatchObject({ compact: false, drawer: false })
    expect(resolveLayout(1279, defaultLayout)).toMatchObject({ compact: true, sidebar: 48 })
    expect(resolveLayout(1100, defaultLayout).drawer).toBe(false)
    expect(resolveLayout(1099, defaultLayout).drawer).toBe(true)
    expect(resolveLayout(1440, defaultLayout)).toMatchObject({ sidebar: 260, chat: 420 })
    expect(defaultLayout.sidebarPx).toBe(260)
  })

  it('gives explicit hiding priority and leaves room for the minimum workspace', () => {
    expect(resolveLayout(1200, { ...defaultLayout, sidebarHidden: true }).sidebar).toBe(0)
    for (const width of [1280, 1440, 1728]) {
      const result = resolveLayout(width, { ...defaultLayout, railPx: 64, sidebarPx: 280, chatPx: 640 })
      expect(width - result.rail - result.sidebar - result.chat - 27).toBeGreaterThanOrEqual(520)
    }
  })
})
