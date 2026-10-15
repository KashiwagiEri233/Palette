export interface LayoutPreferences {
  version: 1
  railPx: number
  sidebarPx: number
  chatPx: number
  sidebarHidden: boolean
  workspaceHidden: boolean
}

export const defaultLayout: LayoutPreferences = { version: 1, railPx: 48, sidebarPx: 260, chatPx: 420, sidebarHidden: false, workspaceHidden: false }
export const layoutStorageKey = 'palette.layout.v1'
const clamp = (value: unknown, fallback: number, min: number, max: number): number => typeof value === 'number' && Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : fallback

export function parseLayout(source: string | null): LayoutPreferences {
  try {
    const data = JSON.parse(source ?? '{}')
    if (!data || data.version !== 1) return { ...defaultLayout }
    return { version: 1, railPx: clamp(data.railPx, 48, 48, 64), sidebarPx: clamp(data.sidebarPx, 260, 240, 280), chatPx: clamp(data.chatPx, 420, 360, 640), sidebarHidden: data.sidebarHidden === true, workspaceHidden: data.workspaceHidden === true }
  } catch { return { ...defaultLayout } }
}

export function resolveLayout(width: number, preferences: LayoutPreferences) {
  const drawer = width < 1100
  const compact = width < 1280 || width - preferences.railPx - preferences.sidebarPx - 360 - 27 < 520
  const rail = compact ? 48 : preferences.railPx
  const sidebar = preferences.sidebarHidden ? 0 : compact ? 48 : preferences.sidebarPx
  const available = Math.max(1, width - rail - sidebar - (sidebar ? 2 : 1))
  const chat = Math.min(preferences.chatPx, Math.max(360, available - 545))
  return { drawer, compact, rail, sidebar, chat, available }
}
