import { Fragment, type ComponentProps, type ReactNode } from 'react'
import { MoreHorizontal, Play, Power, RotateCw, Pause, Settings2, Trash2, Square, RotateCcw, Disc } from '@/components/icons/workbench'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator } from '@/components/ui/dropdown-menu'
import type { VmRecord } from '../../shared/types'

export const stateNames: Record<string, string> = { STOPPED: '已关机', STARTING: '启动中', RUNNING: '运行中', STOPPING: '关机中', PAUSED: '已暂停', FAILED: '故障' }
export { activeVm, errorText, isShutdownTimeout, isShutdownFailure } from '../lib/vm-lifecycle'
import { activeVm } from '../lib/vm-lifecycle'

export function IconButton({ label, children, ...props }: Omit<ComponentProps<typeof Button>, 'aria-label'> & { label: string; children: ReactNode }) {
  return <Tooltip><TooltipTrigger render={<Button variant="ghost" size="icon-sm" aria-label={label} {...props} />}>{children}</TooltipTrigger><TooltipContent>{label}</TooltipContent></Tooltip>
}

export function VmStatusBadge({ vm }: { vm: VmRecord }) {
  return <Badge variant={vm.status.state === 'RUNNING' ? 'success' : vm.status.state === 'FAILED' ? 'destructive' : 'secondary'}><span className={`status-dot ${vm.status.state === 'RUNNING' ? 'online' : ''}`} />{stateNames[vm.status.state] ?? '未知状态'}</Badge>
}

export function VmActions({ vm, busy, onAction, compact = false, inline = false }: { vm: VmRecord; busy: boolean; onAction: (vm: VmRecord, action: string) => void; compact?: boolean; inline?: boolean }) {
  const state = vm.status.state
  const active = activeVm(vm)
  const actions = [
    { action: 'start', label: '启动', Icon: Play, disabled: active },
    { action: 'power', label: '关机', Icon: Power, disabled: !active, title: '强制停止虚拟机，需确认' },
    { action: 'stop', label: '请求正常关机', Icon: Power, disabled: !['RUNNING', 'PAUSED'].includes(state) },
    { action: 'restart', label: '重启', Icon: RotateCw, disabled: state !== 'RUNNING' || !vm.persistent },
    { action: 'pause', label: '暂停', Icon: Pause, disabled: true, title: '当前版本暂不支持暂停' },
    { action: 'settings', label: '虚拟机设置', Icon: Settings2, disabled: !vm.persistent },
    { action: 'tools.mount', label: '安装增强工具与驱动', Icon: Disc, disabled: !vm.persistent || vm.definition?.draft.os_preset === 'linux' || vm.status.guest_arch !== 'x86_64' || !['RUNNING', 'STOPPED', 'FAILED'].includes(state) },
    { action: 'tools.eject', label: '弹出增强工具光盘', Icon: Disc, disabled: !vm.definition?.tools_iso_attached || !['RUNNING', 'STOPPED', 'FAILED'].includes(state) },
    { action: 'force', label: '强制停止', Icon: Square, disabled: !active, destructive: true },
    { action: 'reset', label: '恢复初始状态', Icon: RotateCcw, disabled: active, destructive: true },
    { action: 'delete', label: '删除虚拟机', Icon: Trash2, disabled: active || !vm.persistent, destructive: true },
  ]
  const primaryActions = actions.filter(({ action }) => [active ? 'power' : 'start', 'restart', 'pause', 'settings', 'delete'].includes(action))
  const menuActions = inline ? actions.filter(({ action }) => ['stop', 'tools.mount', 'tools.eject', 'force', 'reset'].includes(action)) : actions

  return <>
    {inline && primaryActions.map(({ action, label, Icon, disabled, title, destructive }) => <Button
      key={action}
      variant="ghost"
      size="sm"
      className={destructive ? 'vm-inline-action text-destructive hover:text-destructive' : 'vm-inline-action'}
      disabled={busy || disabled}
      title={title}
      onClick={action === 'pause' ? undefined : () => onAction(vm, action)}
    ><Icon size={16} />{label}</Button>)}
    <DropdownMenu><DropdownMenuTrigger render={<Button variant="ghost" size={compact ? 'icon' : 'sm'} aria-label={`${vm.name} 的操作`} disabled={busy} />}>
      {compact ? <MoreHorizontal size={16} /> : <>更多 <MoreHorizontal size={16} /></>}
    </DropdownMenuTrigger><DropdownMenuContent align="end">
      {menuActions.map(({ action, label, Icon, disabled, title, destructive }) => <Fragment key={action}>
        {!inline && ['settings', 'force'].includes(action) && <DropdownMenuSeparator />}
        <DropdownMenuItem
          className={destructive ? 'text-destructive' : undefined}
          disabled={busy || disabled}
          title={title}
          onClick={action === 'pause' ? undefined : () => onAction(vm, action)}
        ><Icon size={16} />{label}</DropdownMenuItem>
      </Fragment>)}
    </DropdownMenuContent></DropdownMenu>
  </>
}
