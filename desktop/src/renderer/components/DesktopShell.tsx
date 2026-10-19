import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import type { ImperativePanelHandle } from 'react-resizable-panels'
import { ArrowLeft, ArrowRight, PanelLeft, Search, Menu } from '@/components/icons/workbench'
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from '@/components/ui/resizable'
import { IconButton } from './workspace-primitives'
import { Card } from '@/components/ui/card'
import { defaultLayout, resolveLayout, type LayoutPreferences } from '@/lib/layout'

export function useViewportWidth() {
  const [width, setWidth] = useState(window.innerWidth)
  useLayoutEffect(() => {
    const resize = () => setWidth(window.innerWidth)
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [])
  return width
}

interface Props {
  preferences: LayoutPreferences
  onPreferences: (patch: Partial<LayoutPreferences>) => void
  rail: ReactNode
  navigation: ReactNode
  children: ReactNode
  onSearch(): void
  onBack(): void
  onForward(): void
  canBack: boolean
  canForward: boolean
}

export function DesktopShell({ preferences, onPreferences, rail, navigation, children, onSearch, onBack, onForward, canBack, canForward }: Props) {
  const width = useViewportWidth()
  const hasNavigation = navigation != null
  const sidebarHidden = !hasNavigation || preferences.sidebarHidden
  const layout = resolveLayout(width, { ...preferences, sidebarHidden })
  const railPanel = useRef<ImperativePanelHandle>(null)
  const sidebarPanel = useRef<ImperativePanelHandle>(null)
  const percent = (pixels: number) => pixels / Math.max(1, width - 2) * 100
  const contentWidth = Math.max(1, width - layout.rail - 2)
  const sidebarPercent = (pixels: number) => pixels / contentWidth * 100
  useLayoutEffect(() => {
    railPanel.current?.resize(percent(layout.rail))
    sidebarPanel.current?.resize(sidebarPercent(layout.sidebar))
  }, [width, layout.rail, layout.sidebar])

  return <div className={`desktop-shell ${layout.compact ? 'compact-navigation' : ''}`}>
    <header className="desktop-titlebar">
      <div className="titlebar-history"><IconButton label="后退" disabled={!canBack} onClick={onBack}><ArrowLeft size={16} /></IconButton><IconButton label="前进" disabled={!canForward} onClick={onForward}><ArrowRight size={16} /></IconButton></div>
      {hasNavigation && <IconButton label={preferences.sidebarHidden ? '展开侧栏' : '收起侧栏'} onClick={() => onPreferences({ sidebarHidden: !preferences.sidebarHidden })}><PanelLeft size={16} /></IconButton>}
      <IconButton label="应用菜单" onClick={() => void window.palette.request('window.command', undefined, { action: 'menu' })}><Menu size={16} /></IconButton>
      <div className="titlebar-center" />
      <IconButton label="搜索虚拟机和会话" onClick={onSearch}><Search size={16} /></IconButton>
    </header>
    <ResizablePanelGroup direction="horizontal" className="desktop-body">
      <ResizablePanel id="global-rail" order={1} ref={railPanel} defaultSize={percent(layout.rail)} minSize={percent(48)} maxSize={percent(layout.compact ? 48 : 64)}>{rail}</ResizablePanel>
      <ResizableHandle variant="chrome" aria-label="调整导航栏宽度" onDoubleClick={() => { railPanel.current?.resize(percent(48)); onPreferences({ railPx: 48 }) }} onDragging={dragging => { if (!dragging && !layout.compact) onPreferences({ railPx: (railPanel.current?.getSize() ?? percent(48)) / 100 * (width - 2) }) }} />
      <ResizablePanel id="content-surface" order={2}>
        <Card variant="workspace" className="desktop-content-surface">
          <ResizablePanelGroup direction="horizontal">
            <ResizablePanel id="navigation" order={1} ref={sidebarPanel} defaultSize={sidebarPercent(layout.sidebar)} minSize={sidebarHidden ? 0 : sidebarPercent(layout.compact ? 48 : 240)} maxSize={sidebarPercent(layout.compact ? 48 : 280)} collapsible collapsedSize={0} className="navigation-panel" style={sidebarHidden ? { display: 'none' } : undefined}>{navigation}</ResizablePanel>
            {!sidebarHidden && <ResizableHandle aria-label="调整侧栏宽度" disabled={layout.compact} onDoubleClick={() => { sidebarPanel.current?.resize(sidebarPercent(defaultLayout.sidebarPx)); onPreferences({ sidebarPx: defaultLayout.sidebarPx }) }} onDragging={dragging => { if (!dragging && !layout.compact) onPreferences({ sidebarPx: (sidebarPanel.current?.getSize() ?? sidebarPercent(260)) / 100 * contentWidth }) }} />}
            <ResizablePanel id="main-content" order={2} minSize={sidebarPercent(layout.drawer ? 240 : 600)}><main className="desktop-main">{children}</main></ResizablePanel>
          </ResizablePanelGroup>
        </Card>
      </ResizablePanel>
    </ResizablePanelGroup>
  </div>
}
