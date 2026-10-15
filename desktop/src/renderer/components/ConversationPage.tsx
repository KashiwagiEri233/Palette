import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ImperativePanelHandle } from 'react-resizable-panels'
import { MoreHorizontal, PanelRightClose, PanelRightOpen, Plus, Sparkles, ChevronDown, Monitor } from '@/components/icons/workbench'
import { Card, CardHeader } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import { AgentChat } from '@/components/ui/agent-chat'
import { PromptInput } from '@/components/ui/ai-agent-input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from '@/components/ui/resizable'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Console } from '../Console'
import { IconButton, VmStatusBadge, VmActions } from './workspace-primitives'
import { useViewportWidth } from './DesktopShell'
import type { Conversation } from '../../shared/workspace'
import type { VmRecord } from '../../shared/types'
import type { LayoutPreferences } from '@/lib/layout'

interface Props {
  conversation: Conversation
  vm?: VmRecord
  preferences: LayoutPreferences
  busy: boolean
  onPreferences(patch: Partial<LayoutPreferences>): void
  onDraft(id: string, value: string): void
  draftState?: 'saving' | 'saved' | 'error'
  onRetrySave(): void
  onRename(): void
  onDelete(): void
  onClear(): void
  onExport(): void
  onAction(vm: VmRecord, action: string): void
  onAgentSettings(): void
  reportError(message: string): void
}

export function ConversationPage(props: Props) {
  const width = useViewportWidth()
  const drawer = width < 1100
  const [drawerOpen, setDrawerOpen] = useState(false)
  const container = useRef<HTMLDivElement>(null)
  const chatPanel = useRef<ImperativePanelHandle>(null)
  const [available, setAvailable] = useState(1000)
  useLayoutEffect(() => {
    if (!container.current) return
    const observer = new ResizeObserver(entries => setAvailable(entries[0].contentRect.width))
    observer.observe(container.current)
    return () => observer.disconnect()
  }, [])
  const showWorkspace = !props.preferences.workspaceHidden && !drawer
  useLayoutEffect(() => {
    if (showWorkspace) chatPanel.current?.resize(Math.max(320, Math.min(props.preferences.chatPx, available - 545)) / available * 100)
  }, [available, showWorkspace, props.preferences.chatPx])
  const toggleWorkspace = () => drawer ? setDrawerOpen(value => !value) : props.onPreferences({ workspaceHidden: !props.preferences.workspaceHidden })
  useEffect(() => {
    const toggle = () => setDrawerOpen(value => !value)
    window.addEventListener('palette-toggle-workspace', toggle)
    return () => window.removeEventListener('palette-toggle-workspace', toggle)
  }, [])
  const vm = props.vm
  const workspace = vm ? <Card variant="pane" className="vm-workspace">
    <CardHeader className="vm-workspace-heading"><Monitor size={16} /><strong>{vm.name}</strong><VmStatusBadge vm={vm} /><span className="workspace-architecture">{vm.status.guest_arch ?? '—'}</span><span className="flex-1" /><VmActions vm={vm} busy={props.busy} compact onAction={props.onAction} /><IconButton label="收起工作区" onClick={() => drawer ? setDrawerOpen(false) : props.onPreferences({ workspaceHidden: true })}><PanelRightClose size={16} /></IconButton></CardHeader>
    <Console key={vm.vm_id} vm={vm} reportError={props.reportError} onAction={action => props.onAction(vm, action)} busy={props.busy} />
  </Card> : <EmptyState variant="plain" icon={<Monitor size={24} />} title="虚拟机不可用" />

  const chat = <AgentChat header={<CardHeader className="conversation-header"><Button variant="ghost" className="conversation-title" size="sm" onClick={props.onRename}>{props.conversation.title}</Button><div className="conversation-header-meta"><Badge variant="secondary"><span className={`status-dot ${vm?.status.state === 'RUNNING' ? 'online' : ''}`} />{vm?.name ?? '虚拟机不可用'}</Badge><span className="flex-1" /><DropdownMenu><DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" aria-label="对话操作" />}><MoreHorizontal size={16} /></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem onClick={props.onExport}>导出对话</DropdownMenuItem><DropdownMenuItem onClick={props.onClear}>清空草稿</DropdownMenuItem><DropdownMenuItem className="text-destructive" onClick={props.onDelete}>删除对话</DropdownMenuItem></DropdownMenuContent></DropdownMenu><IconButton label={showWorkspace ? '隐藏工作区' : '显示工作区'} onClick={toggleWorkspace}>{showWorkspace ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}</IconButton></div></CardHeader>}
    composer={<div className="conversation-composer">
      {vm && vm.status.state !== 'RUNNING' && <div className="composer-notice"><span>虚拟机未运行</span><Button variant="ghost" size="sm" disabled={props.busy || ['STARTING', 'STOPPING', 'PAUSED'].includes(vm.status.state)} onClick={() => props.onAction(vm, 'start')}>启动</Button></div>}
      <div className="composer-notice"><span>Agent 尚未配置</span><Button variant="ghost" size="sm" onClick={props.onAgentSettings}>前往设置</Button></div>
      <PromptInput value={props.conversation.draft} onValueChange={value => props.onDraft(props.conversation.id, value)} onSend={() => {}} sendDisabled disabled={props.conversation.archived || !vm} footer={<><IconButton label="附加文件（尚不可用）" disabled><Plus size={16} /></IconButton><Badge variant="secondary">{vm?.name ?? '虚拟机不可用'}</Badge><Button variant="ghost" size="sm" onClick={props.onAgentSettings}>选择模型<ChevronDown size={12} /></Button></>} />
      <div className="composer-caption" role="status">{props.conversation.archived ? '已归档的对话为只读' : props.draftState === 'error' ? <>草稿保存失败 <Button variant="ghost" size="sm" onClick={props.onRetrySave}>重试保存</Button></> : props.draftState === 'saving' ? '正在保存草稿…' : '草稿已保存在本机'}</div>
    </div>}>
    <ScrollArea className="conversation-scroll"><EmptyState variant="plain" heading="h1" className="conversation-empty" icon={<Sparkles size={22} />} title={props.conversation.archived ? '对话已归档' : '开始一项新任务'} description={props.conversation.archived ? '可以从搜索中找到此对话。' : '描述任务，或在右侧接管虚拟机。'} /></ScrollArea>
  </AgentChat>

  return <div ref={container} className="conversation-page" data-workspace-mode={drawer ? 'drawer' : showWorkspace ? 'inline' : 'hidden'}>
    {showWorkspace ? <ResizablePanelGroup direction="horizontal"><ResizablePanel ref={chatPanel} id="conversation" defaultSize={Math.min(props.preferences.chatPx, available - 544) / available * 100} minSize={Math.min(360 / available * 100, 48)}>{chat}</ResizablePanel><ResizableHandle aria-label="调整对话区宽度" onDoubleClick={() => { chatPanel.current?.resize(420 / available * 100); props.onPreferences({ chatPx: 420 }) }} onDragging={dragging => { if (!dragging) props.onPreferences({ chatPx: (chatPanel.current?.getSize() ?? 42) / 100 * available }) }} /><ResizablePanel id="vm-workspace" minSize={Math.min(544 / available * 100, 60)}><div className="workspace-inset">{workspace}</div></ResizablePanel></ResizablePanelGroup> : chat}
    {drawer && <Dialog open={drawerOpen} onOpenChange={setDrawerOpen}><DialogContent className="workspace-drawer"><DialogTitle className="sr-only">虚拟机工作区</DialogTitle>{workspace}</DialogContent></Dialog>}
  </div>
}
