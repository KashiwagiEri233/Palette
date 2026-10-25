import type { VmRecord } from '../shared/types'
import { DriveSummary } from './DriveSummary'
import { displayNames } from './lib/vm-settings'
import { stateNames } from './components/workspace-primitives'
const networkNames = { 'restricted-lab': '隔离网络', offline: '离线', nat: 'NAT' }

export function LiveStatus({ vm, updatedAt }: { vm: VmRecord; updatedAt: Date | null }) {
  const status = vm.status
  const controlNames: Record<string, string> = { available: '可操作', held: '已接管', releasing: '正在释放', faulted: '释放未确认，暂停写入' }
  return <section className="live-status" aria-label="虚拟机实时状态"><div className="live-status-heading"><h2>运行状态</h2><span>每 2 秒更新{updatedAt && ` · ${updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}`}</span></div><dl className="configuration-grid"><div><dt>虚拟机标识</dt><dd className="font-mono">{vm.vm_id}</dd></div><div><dt>当前实例</dt><dd className="font-mono">{status.instance_id ?? '尚未启动'}</dd></div><div><dt>运行状态</dt><dd>{stateNames[status.state] ?? status.state}</dd></div><div><dt>控制权</dt><dd data-testid="control-owner">{status.instance_id ? status.control_owner === 'operator' ? '人工' : 'Agent' : '—'}{status.control_state && ` / ${controlNames[status.control_state] ?? status.control_state}`}</dd></div><div><dt>操作序号</dt><dd data-testid="action-seq" className="font-mono">{status.action_seq ?? 0}</dd></div><div><dt>排队任务</dt><dd>{status.queued_actions ?? 0}</dd></div><div><dt>管理通道</dt><dd>{status.qmp_ready ? '已连接' : '未就绪'}</dd></div><div><dt>画面</dt><dd>{status.screen_ready ? '已就绪' : '未就绪'}{status.display && ` / ${status.display.width} × ${status.display.height}`}</dd></div><div><dt>客户机桌面</dt><dd>{status.desktop_ready == null ? '尚未确认' : status.desktop_ready ? '已就绪' : '未就绪'}</dd></div><div><dt>代次（启动 / 显示 / 控制）</dt><dd className="font-mono">{status.boot_epoch ?? 0} / {status.display_epoch ?? 0} / {status.controller_epoch ?? 0}</dd></div><div><dt>保持的按键</dt><dd data-testid="held-keys">{status.input_ledger?.keys.join(', ') || '无'}</dd></div><div><dt>保持的鼠标按钮</dt><dd>{status.input_ledger?.buttons.join(', ') || '无'}</dd></div></dl>{status.input_ledger?.release_confirmed === false && <p role="alert" className="text-destructive">输入释放尚未确认</p>}{status.last_error && <p role="alert" className="text-destructive">{status.last_error.code}: {status.last_error.message}</p>}</section>
}

export function Configuration({ vm }: { vm: VmRecord }) {
  const draft = vm.definition?.draft
  const hardware = draft?.settings
  return <dl className="configuration-grid"><div><dt>系统预设</dt><dd>{{ windows: 'Windows', linux: 'Linux', custom: '自定义' }[draft?.os_preset ?? 'custom']}</dd></div><div><dt>增强工具光盘</dt><dd>{vm.definition?.tools_iso_attached ? '已装载' : '未装载'}</dd></div><div><dt>标识</dt><dd className="font-mono">{vm.vm_id}</dd></div><div><dt>CPU</dt><dd>{draft?.vcpus ?? '—'} 核</dd></div><div><dt>内存</dt><dd>{draft ? draft.memory_mib / 1024 : '—'} GiB</dd></div><div><dt>硬盘数量</dt><dd>{hardware ? `${hardware.drives?.filter(drive => drive.media === 'disk').length ?? 0} 块` : '—'}</dd></div><div><dt>网络</dt><dd>{draft ? networkNames[draft.network] : '—'}</dd></div><div><dt>磁盘模式</dt><dd>{vm.persistent ? '保留修改' : '临时桌面'}</dd></div>{hardware && <><div><dt>虚拟显卡</dt><dd>{displayNames[hardware.display.model] ?? hardware.display.model}</dd></div><div><dt>默认分辨率</dt><dd>{hardware.display.default_resolution ? `${hardware.display.width} × ${hardware.display.height}` : '由固件或系统决定'}</dd></div><div><dt>声音</dt><dd>{hardware.audio_enabled ? '启用' : '关闭'}</dd></div><div><dt>启动顺序</dt><dd>{{ auto: '自动', disk: '硬盘优先', cdrom: '光盘优先' }[hardware.boot_order]}</dd></div></>}<DriveSummary drives={hardware?.drives ?? []} />{draft && <div className="col-span-2"><dt>原始来源</dt><dd className="path-value">{draft.path}</dd></div>}</dl>
}

export { CreateVm } from './CreateVm'
