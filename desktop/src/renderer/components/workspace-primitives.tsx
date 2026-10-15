import type { ComponentProps, ReactNode } from 'react'
import { MoreHorizontal, Play, Power, RotateCw, Pause, Settings2, Trash2, Square, RotateCcw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from '@/components/ui/dropdown-menu'
import type { VmRecord } from '../../shared/types'

export const stateNames: Record<string, string> = { STOPPED: '已关机', STARTING: '启动中', RUNNING: '运行中', STOPPING: '关机中', PAUSED: '已暂停', FAILED: '故障' }
export function errorText(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.replace(/^(?:Error: )?Error invoking remote method '[^']+': Error: /, '').replace(/^Error: /, '')
}
export const activeVm = (vm: VmRecord): boolean => !['STOPPED', 'FAILED'].includes(vm.status.state)

export function IconButton({ label, children, ...props }: Omit<ComponentProps<typeof Button>, 'aria-label'> & { label: string; children: ReactNode }) {
  return <Tooltip><TooltipTrigger render={<Button variant="ghost" size="icon" aria-label={label} {...props} />}>{children}</TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>
}

export function VmStatusBadge({ vm }: { vm: VmRecord }) {
  return <Badge variant={vm.status.state === 'RUNNING' ? 'success' : vm.status.state === 'FAILED' ? 'destructive' : 'secondary'}><span className={`status-dot ${vm.status.state === 'RUNNING' ? 'online' : ''}`} />{stateNames[vm.status.state] ?? '未知状态'}</Badge>
}

export function VmActions({ vm, busy, onAction, compact = false }: { vm: VmRecord; busy: boolean; onAction: (vm: VmRecord, action: string) => void; compact?: boolean }) {
  const state = vm.status.state
  return <DropdownMenu><DropdownMenuTrigger render={<Button variant="ghost" size={compact ? 'icon' : 'sm'} aria-label={`${vm.name} 的操作`} disabled={busy} />}>
    {compact ? <MoreHorizontal size={16} /> : <>更多 <MoreHorizontal size={16} /></>}
  </DropdownMenuTrigger><DropdownMenuContent align="end">
    <DropdownMenuItem disabled={activeVm(vm)} onClick={() => onAction(vm, 'start')}><Play size={16} />启动</DropdownMenuItem>
    <DropdownMenuItem disabled={!['RUNNING', 'PAUSED'].includes(state)} onClick={() => onAction(vm, 'stop')}><Power size={16} />关机</DropdownMenuItem>
    <DropdownMenuItem disabled={state !== 'RUNNING' || !vm.persistent} onClick={() => onAction(vm, 'restart')}><RotateCw size={16} />重启</DropdownMenuItem>
    <DropdownMenuItem disabled title="当前版本暂不支持暂停"><Pause size={16} />暂停</DropdownMenuItem>
    <DropdownMenuSeparator />
    <DropdownMenuItem disabled={!vm.persistent} onClick={() => onAction(vm, 'settings')}><Settings2 size={16} />虚拟机设置</DropdownMenuItem>
    <DropdownMenuSeparator />
    <DropdownMenuItem className="text-destructive" disabled={!activeVm(vm)} onClick={() => onAction(vm, 'force')}><Square size={16} />强制停止</DropdownMenuItem>
    <DropdownMenuItem className="text-destructive" disabled={activeVm(vm)} onClick={() => onAction(vm, 'reset')}><RotateCcw size={16} />恢复初始状态</DropdownMenuItem>
    <DropdownMenuItem className="text-destructive" disabled={activeVm(vm) || !vm.persistent} onClick={() => onAction(vm, 'delete')}><Trash2 size={16} />删除虚拟机</DropdownMenuItem>
  </DropdownMenuContent></DropdownMenu>
}
