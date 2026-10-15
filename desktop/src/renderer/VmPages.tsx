import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/ui/native-select'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { FolderIcon } from '@/components/icons'
import { HardwareFields, ResourceInputs } from './VmSettings'
import { profileSettings, validateVmSettings, sections, type SettingsSection } from './lib/vm-settings'
import type { Profile, VmRecord, VmDraft, HardwareCapabilities } from '../shared/types'
import { stateNames } from './components/workspace-primitives'
const errorText = (error: unknown): string => error instanceof Error ? error.message.replace(/^Error invoking remote method '[^']+': Error: /, '') : String(error)
const networkNames = { 'restricted-lab': '隔离网络', offline: '离线', nat: 'NAT' }

export function LiveStatus({ vm, updatedAt }: { vm: VmRecord; updatedAt: Date | null }) {
  const status = vm.status
  const controlNames: Record<string, string> = { available: '可操作', held: '已接管', releasing: '正在释放', faulted: '释放未确认，暂停写入' }
  return <section className="live-status" aria-label="虚拟机实时状态"><div className="live-status-heading"><h2>运行状态</h2><span>每 2 秒更新{updatedAt && ` · ${updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}`}</span></div><dl className="configuration-grid"><div><dt>虚拟机标识</dt><dd className="font-mono">{vm.vm_id}</dd></div><div><dt>当前实例</dt><dd className="font-mono">{status.instance_id ?? '尚未启动'}</dd></div><div><dt>运行状态</dt><dd>{stateNames[status.state] ?? status.state}</dd></div><div><dt>控制权</dt><dd data-testid="control-owner">{status.instance_id ? status.control_owner === 'operator' ? '人工' : 'Agent' : '—'}{status.control_state && ` / ${controlNames[status.control_state] ?? status.control_state}`}</dd></div><div><dt>操作序号</dt><dd data-testid="action-seq" className="font-mono">{status.action_seq ?? 0}</dd></div><div><dt>排队任务</dt><dd>{status.queued_actions ?? 0}</dd></div><div><dt>管理通道</dt><dd>{status.qmp_ready ? '已连接' : '未就绪'}</dd></div><div><dt>画面</dt><dd>{status.screen_ready ? '已就绪' : '未就绪'}{status.display && ` / ${status.display.width} × ${status.display.height}`}</dd></div><div><dt>客户机桌面</dt><dd>{status.desktop_ready == null ? '尚未确认' : status.desktop_ready ? '已就绪' : '未就绪'}</dd></div><div><dt>代次（启动 / 显示 / 控制）</dt><dd className="font-mono">{status.boot_epoch ?? 0} / {status.display_epoch ?? 0} / {status.controller_epoch ?? 0}</dd></div><div><dt>保持的按键</dt><dd data-testid="held-keys">{status.input_ledger?.keys.join(', ') || '无'}</dd></div><div><dt>保持的鼠标按钮</dt><dd>{status.input_ledger?.buttons.join(', ') || '无'}</dd></div></dl>{status.input_ledger?.release_confirmed === false && <p role="alert" className="text-destructive">输入释放尚未确认</p>}{status.last_error && <p role="alert" className="text-destructive">{status.last_error.code}: {status.last_error.message}</p>}</section>
}

export function Configuration({ vm }: { vm: VmRecord }) {
  const draft = vm.definition?.draft
  const hardware = draft?.settings
  const iso = hardware?.iso_path ?? (draft?.source === 'iso' ? draft.path : null)
  return <dl className="configuration-grid"><div><dt>标识</dt><dd className="font-mono">{vm.vm_id}</dd></div><div><dt>CPU</dt><dd>{draft?.vcpus ?? '—'} 核</dd></div><div><dt>内存</dt><dd>{draft ? draft.memory_mib / 1024 : '—'} GiB</dd></div><div><dt>磁盘容量</dt><dd>{draft?.disk_gib ?? '—'} GiB</dd></div><div><dt>网络</dt><dd>{draft ? networkNames[draft.network] : '—'}</dd></div><div><dt>磁盘模式</dt><dd>{vm.persistent ? '保留修改' : '临时桌面'}</dd></div>{hardware && <><div><dt>虚拟显卡</dt><dd>{hardware.display.model}</dd></div><div><dt>默认分辨率</dt><dd>{hardware.display.default_resolution ? `${hardware.display.width} × ${hardware.display.height}` : '由固件或系统决定'}</dd></div><div><dt>声音</dt><dd>{hardware.audio_enabled ? '启用' : '关闭'}</dd></div><div><dt>启动顺序</dt><dd>{{ auto: '自动', disk: '硬盘优先', cdrom: '光盘优先' }[hardware.boot_order]}</dd></div></>}{iso && <div className="col-span-2"><dt>安装介质 · {vm.definition?.iso_attached ? '已挂载' : '未挂载'}</dt><dd className="path-value">{iso}</dd></div>}{draft && <div className="col-span-2"><dt>原始来源</dt><dd className="path-value">{draft.path}</dd></div>}</dl>
}

export function CreateVm({ open, source, onClose, profiles, defaultProfile, advancedSupported, onCreated }: { open: boolean; source: 'image' | 'iso'; onClose: () => void; profiles: Profile[]; defaultProfile: string; advancedSupported: boolean; onCreated: (id: string) => Promise<void> }) {
  const [step, setStep] = useState(0)
  const [draft, setDraft] = useState<VmDraft>({ name: '', profile_id: '', source: 'image', path: '', image_format: 'qcow2', vcpus: 2, memory_mib: 4096, disk_gib: 24, network: 'restricted-lab' })
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [advanced, setAdvanced] = useState(false)
  const [hardwareSection, setHardwareSection] = useState<SettingsSection>('display')
  const [capabilities, setCapabilities] = useState<HardwareCapabilities | null>(null)
  const [isoAttached, setIsoAttached] = useState(true)
  const [leaveCreate, setLeaveCreate] = useState(false)
  useEffect(() => { if (open) { setAdvanced(false); setIsoAttached(true); setLeaveCreate(false) } }, [open])
  function close(): void { if (!saving) { if (draft.path || draft.name) setLeaveCreate(true); else onClose() } }
  useEffect(() => {
    if (!open || !advancedSupported || !draft.profile_id) return
    let live = true
    const profile = profiles.find(item => item.profile_id === draft.profile_id)
    if (profile) setDraft(previous => ({ ...previous, settings: profileSettings(profile, previous.source === 'iso' ? previous.path || null : null) }))
    setCapabilities(null)
    void window.palette.request('hardware.capabilities', undefined, { profile_id: draft.profile_id }).then(data => { if (live) setCapabilities(data as HardwareCapabilities) }).catch(error => { if (live) setError(errorText(error)) })
    return () => { live = false }
  }, [open, draft.profile_id, advancedSupported, profiles])
  useEffect(() => { if (draft.source === 'iso') setDraft(previous => previous.settings ? ({ ...previous, settings: { ...previous.settings, iso_path: previous.path || null } }) : previous) }, [draft.path, draft.source])
  useEffect(() => { if (open) { setStep(0); setError(''); setHardwareSection('display'); const profile = profiles.find(item => item.profile_id === defaultProfile) ?? profiles[0]; setDraft({ name: '', profile_id: profile?.profile_id ?? '', source, path: '', image_format: 'qcow2', vcpus: profile?.resources.vcpus ?? 2, memory_mib: profile?.resources.memory_mib ?? 4096, disk_gib: profile?.images.size_gib ?? 24, network: profile && ['offline', 'nat', 'restricted-lab'].includes(profile.network.mode) ? profile.network.mode as VmDraft['network'] : 'restricted-lab', settings: advancedSupported && profile ? profileSettings(profile) : undefined }) } }, [open, source])
  const change = <K extends keyof VmDraft>(key: K, value: VmDraft[K]): void => setDraft(previous => ({ ...previous, [key]: value }))
  async function create(): Promise<void> {
    const invalid = validateVmSettings(draft, draft.settings, isoAttached)
    if (invalid) { setStep(1); if (['disk', 'display', 'network', 'audio'].includes(invalid.section)) { setAdvanced(true); setHardwareSection(invalid.section) }; setLeaveCreate(false); setError(invalid.message); requestAnimationFrame(() => document.getElementById(invalid.field === 'edit-name' ? 'vm-name' : invalid.field)?.focus()); return }
    setSaving(true); setError('')
    try { const result = await window.palette.request('vms.create', undefined, { ...draft, settings: advancedSupported ? draft.settings : undefined, iso_attached: advancedSupported ? isoAttached : undefined }) as { vm_id: string }; await onCreated(result.vm_id) }
    catch (error) { setError(errorText(error)) }
    finally { setSaving(false) }
  }
  return <><Dialog open={open} onOpenChange={value => { if (!value) close() }}><DialogContent className="create-dialog"><DialogHeader><DialogTitle>{['选择来源', '配置虚拟机', '确认创建'][step]}</DialogTitle><DialogDescription>{['选择已有磁盘镜像，或使用 ISO 安装系统。', '选择匹配系统架构的配置与资源。', '创建后可以启动虚拟机并打开控制台。'][step]}</DialogDescription></DialogHeader>
    <ol className="wizard-steps" aria-label="创建步骤">{['来源', '配置', '确认'].map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined} className={index <= step ? 'reached' : ''}><span>{index + 1}</span>{label}</li>)}</ol>
    {step === 0 ? <div className="form-stack"><div className="source-options"><Button variant={draft.source === 'image' ? 'default' : 'secondary'} onClick={() => { change('source', 'image'); change('path', '') }}>磁盘镜像</Button><Button variant={draft.source === 'iso' ? 'default' : 'secondary'} onClick={() => { change('source', 'iso'); change('path', '') }}>ISO 安装</Button></div><div><Label htmlFor="source-path">{draft.source === 'iso' ? 'ISO 文件' : '镜像文件'}</Label><div className="file-field"><Input id="source-path" value={draft.path} readOnly placeholder="选择文件" /><Button variant="secondary" onClick={() => void window.palette.chooseFile(draft.source).then(path => { if (path) change('path', path) }).catch(error => setError(errorText(error)))}><FolderIcon className="size-4" />浏览</Button></div></div>{draft.source === 'image' && <div><Label htmlFor="image-format">镜像格式</Label><NativeSelect id="image-format" value={draft.image_format} onChange={event => change('image_format', event.target.value as 'qcow2' | 'raw')}><option value="qcow2">qcow2</option><option value="raw">raw</option></NativeSelect></div>}</div> : step === 1 ? <div className="form-stack"><div><Label htmlFor="vm-name">名称</Label><Input id="vm-name" autoFocus maxLength={120} value={draft.name} onChange={event => change('name', event.target.value)} placeholder="例如：开发环境" /></div><div><Label htmlFor="vm-profile">系统配置</Label><NativeSelect id="vm-profile" value={draft.profile_id} onChange={event => change('profile_id', event.target.value)}>{profiles.map(profile => <option key={profile.profile_id} value={profile.profile_id}>{profile.profile_id} / {profile.qemu.arch} / {profile.qemu.accelerator}</option>)}</NativeSelect></div><ResourceFields draft={draft} change={change} /></div> : <dl className="configuration-grid"><div><dt>名称</dt><dd>{draft.name}</dd></div><div><dt>配置</dt><dd>{draft.profile_id}</dd></div><div><dt>CPU / 内存</dt><dd>{draft.vcpus} 核 / {draft.memory_mib / 1024} GiB</dd></div><div><dt>磁盘容量</dt><dd>{draft.disk_gib} GiB</dd></div><div><dt>网络</dt><dd>{networkNames[draft.network]}</dd></div><div><dt>磁盘模式</dt><dd>保留修改</dd></div><div className="col-span-2"><dt>来源</dt><dd className="path-value">{draft.path}</dd></div></dl>}
    {step === 1 && advancedSupported && <div className="create-advanced"><Button variant="ghost" aria-expanded={advanced} onClick={() => setAdvanced(value => !value)}>{advanced ? '收起硬件选项' : '更多硬件选项'}</Button>{advanced && draft.settings && <><div className="hardware-tabs">{sections.filter(item => ['disk', 'display', 'network', 'audio'].includes(item.id)).map(item => <Button key={item.id} variant="ghost" aria-pressed={hardwareSection === item.id} onClick={() => setHardwareSection(item.id)}>{item.label}</Button>)}</div><HardwareFields section={hardwareSection} settings={draft.settings} change={settings => change('settings', settings)} caps={capabilities} attached={isoAttached} setAttached={setIsoAttached} draft={draft} setDraft={setDraft} includeMode={false} /></>}</div>}
    {step === 2 && draft.settings && <dl className="configuration-grid" aria-label="硬件摘要"><div><dt>显示</dt><dd>{draft.settings.display.model}</dd></div><div><dt>声音</dt><dd>{draft.settings.audio_enabled ? '启用' : '关闭'}</dd></div><div><dt>端口转发</dt><dd>{draft.settings.network.forwards.length} 条</dd></div><div><dt>启动顺序</dt><dd>{{ auto: '自动', disk: '硬盘优先', cdrom: '光盘优先' }[draft.settings.boot_order]}</dd></div>{draft.settings.iso_path && <div className="col-span-2"><dt>安装介质 · {isoAttached ? '已挂载' : '未挂载'}</dt><dd className="path-value">{draft.settings.iso_path}</dd></div>}</dl>}
    {error && <p className="text-sm text-destructive" role="alert">{error}</p>}<DialogFooter><Button variant="secondary" disabled={saving} onClick={() => step ? setStep(step - 1) : close()}>{step ? '上一步' : '取消'}</Button>{step < 2 ? <Button disabled={step === 0 ? !draft.path : !draft.name.trim() || !draft.profile_id || !Number.isInteger(draft.vcpus) || draft.vcpus < 1 || draft.vcpus > 64 || draft.memory_mib < 256 || draft.disk_gib < 1} onClick={() => setStep(step + 1)}>下一步</Button> : <Button disabled={saving} onClick={() => void create()}>{saving ? '创建中…' : '创建虚拟机'}</Button>}</DialogFooter></DialogContent></Dialog>
    <Dialog open={leaveCreate} onOpenChange={setLeaveCreate}><DialogContent><DialogHeader><DialogTitle>保留新建配置？</DialogTitle><DialogDescription>可以继续编辑、丢弃配置，或直接创建虚拟机。</DialogDescription></DialogHeader><DialogFooter><Button variant="ghost" disabled={saving} onClick={() => setLeaveCreate(false)}>继续编辑</Button><Button variant="secondary" disabled={saving} onClick={() => { setLeaveCreate(false); onClose() }}>丢弃配置</Button><Button disabled={saving || !draft.path || !draft.name.trim()} onClick={() => { setLeaveCreate(false); void create() }}>创建虚拟机</Button></DialogFooter></DialogContent></Dialog></>
}

function ResourceFields({ draft, change, disk = true }: { draft: VmDraft; change: <K extends keyof VmDraft>(key: K, value: VmDraft[K]) => void; disk?: boolean }) {
  return <><ResourceInputs draft={draft} change={change} />{disk && <div><Label htmlFor="disk">磁盘 / GiB</Label><Input id="disk" type="number" min={1} max={4096} value={draft.disk_gib} onChange={event => change('disk_gib', Number(event.target.value))} /></div>}<div><Label htmlFor="network">网络</Label><NativeSelect id="network" value={draft.network} onChange={event => change('network', event.target.value as VmDraft['network'])}><option value="restricted-lab">隔离网络</option><option value="offline">离线</option><option value="nat">NAT</option></NativeSelect></div></>
}
