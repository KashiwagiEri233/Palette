import { useState } from 'react'
import { ChevronDown, ChevronRight, MessageSquare, Plus, Search, Pin, MoreHorizontal } from '@/components/icons/workbench'
import { Sidebar, SidebarHeader, SidebarContent, SidebarFooter, SidebarGroup, SidebarItem, SidebarActionRow, SidebarRowActions } from '@/components/ui/sidebar'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from '@/components/ui/context-menu'
import { IconButton, stateNames, VmActions } from './workspace-primitives'
import type { Conversation, ConversationPatch } from '../../shared/workspace'
import type { VmRecord } from '../../shared/types'

interface Props {
  vms: VmRecord[]
  conversations: Conversation[]
  selectedId?: string
  compact: boolean
  connection: string
  busy: boolean
  onNew(vmId?: string): void
  onSelect(item: Conversation): void
  onOpenVm(vm: VmRecord): void
  onVmAction(vm: VmRecord, action: string): void
  onUpdate(item: Conversation, patch: ConversationPatch): void
  onRename(item: Conversation): void
  onDelete(item: Conversation): void
  onSearch(): void
  onManage(): void
}

export function ConversationNavigation(props: Props) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const visible = props.conversations.filter(item => !item.archived)
  const ordered = [...visible].sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.updatedAt.localeCompare(a.updatedAt))
  const row = (item: Conversation) => <ContextMenu key={item.id}><ContextMenuTrigger render={<div className="conversation-row-wrap" />}>
    <SidebarItem density="compact" active={props.selectedId === item.id} aria-current={props.selectedId === item.id ? 'page' : undefined} title={item.title} icon={item.pinned ? <Pin size={12} /> : <MessageSquare size={14} />} onClick={() => props.onSelect(item)}>{item.title}</SidebarItem>
  </ContextMenuTrigger><ContextMenuContent>
    <ContextMenuItem onClick={() => props.onRename(item)}>重命名</ContextMenuItem>
    <ContextMenuItem onClick={() => props.onUpdate(item, { pinned: !item.pinned })}>{item.pinned ? '取消置顶' : '置顶'}</ContextMenuItem>
    <ContextMenuItem onClick={() => props.onUpdate(item, { archived: true })}>归档</ContextMenuItem>
    <ContextMenuItem className="text-destructive" onClick={() => props.onDelete(item)}>删除对话</ContextMenuItem>
  </ContextMenuContent></ContextMenu>

  if (props.compact) return <Sidebar variant="inset" className="app-sidebar collapsed-vm-navigation"><IconButton label="新对话" disabled={props.busy} onClick={() => props.onNew()}><Plus size={16} /></IconButton>
    <SidebarContent>{props.vms.map(vm => <DropdownMenu key={vm.vm_id}><DropdownMenuTrigger render={<Button variant="ghost" size="icon" aria-label={`${vm.name} 的会话`} title={vm.name} />}><span className="vm-initial">{vm.name.slice(0, 1)}</span></DropdownMenuTrigger><DropdownMenuContent side="right">
      <DropdownMenuItem onClick={() => props.onNew(vm.vm_id)}>新对话</DropdownMenuItem>
      <DropdownMenuItem onClick={() => props.onOpenVm(vm)}>查看虚拟机</DropdownMenuItem>
      {ordered.filter(item => item.vm_id === vm.vm_id).map(item => <DropdownMenuItem key={item.id} onClick={() => props.onSelect(item)}>{item.title}</DropdownMenuItem>)}
    </DropdownMenuContent></DropdownMenu>)}</SidebarContent><IconButton label="搜索" onClick={props.onSearch}><Search size={16} /></IconButton></Sidebar>

  return <Sidebar variant="inset" className="app-sidebar conversation-navigation">
    <SidebarHeader separator={false} className="navigation-brand"><span className="navigation-brand-name">Palette</span><IconButton label="搜索" onClick={props.onSearch}><Search size={16} /></IconButton></SidebarHeader>
    <div className="new-conversation"><SidebarItem density="compact" className="h-10" icon={<Plus size={16} />} disabled={props.busy} onClick={() => props.onNew()}>新对话</SidebarItem></div>
    <SidebarContent><SidebarGroup><div className="nav-section-label">虚拟机</div>{props.vms.length === 0 && <p className="nav-empty">暂无虚拟机</p>}{[...props.vms].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN')).map(vm => {
      const items = ordered.filter(item => item.vm_id === vm.vm_id)
      const isCollapsed = collapsed.has(vm.vm_id)
      return <div key={vm.vm_id} className={`vm-conversation-group ${vm.status.state === 'STOPPED' ? 'vm-is-stopped' : ''}`}>
        <SidebarActionRow className="vm-group-heading"><Button variant="ghost" size="sm" className="vm-group-toggle" aria-label={`${isCollapsed ? '展开' : '折叠'} ${vm.name}`} aria-expanded={!isCollapsed} onClick={() => setCollapsed(previous => { const next = new Set(previous); if (next.has(vm.vm_id)) next.delete(vm.vm_id); else next.add(vm.vm_id); return next })}>{isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}<span className={`status-dot ${vm.status.state === 'RUNNING' ? 'online' : ''}`} /><span className="vm-group-name">{vm.name}</span><small data-slot="sidebar-row-status">{stateNames[vm.status.state]}</small></Button>
          <SidebarRowActions className="vm-group-quick"><IconButton label={`在 ${vm.name} 下新建对话`} onClick={() => props.onNew(vm.vm_id)}><Plus size={14} /></IconButton><VmActions vm={vm} busy={props.busy} compact onAction={props.onVmAction} /></SidebarRowActions>
        </SidebarActionRow>
        {!isCollapsed && <div className="vm-conversations">{items.slice(0, expanded.has(vm.vm_id) ? undefined : 5).map(item => row(item))}{!items.length && <span className="nav-empty">暂无对话</span>}{items.length > 5 && <Button variant="ghost" size="sm" onClick={() => setExpanded(previous => { const next = new Set(previous); if (next.has(vm.vm_id)) next.delete(vm.vm_id); else next.add(vm.vm_id); return next })}>{expanded.has(vm.vm_id) ? '收起显示' : `展开显示（${items.length - 5}）`}</Button>}</div>}
      </div>
    })}</SidebarGroup></SidebarContent>
    <SidebarFooter><span className="navigation-connection">{props.connection}</span><IconButton label="管理虚拟机" onClick={props.onManage}><MoreHorizontal size={16} /></IconButton></SidebarFooter>
  </Sidebar>
}
