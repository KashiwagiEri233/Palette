import { useCallback, useEffect, useState } from 'react'
import { Sidebar, SidebarHeader, SidebarContent, SidebarFooter, SidebarGroup, SidebarItem } from '@/components/ui/sidebar'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/ui/native-select'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { ComputerIcon, PlusIcon, SettingsIcon, PlayIcon, FolderIcon, ArrowLeftIcon, SearchIcon } from '@/components/icons'
import { Console } from './Console'
import { VmSettings, HardwareFields, ResourceInputs } from './VmSettings'
import { profileSettings, validateVmSettings, sections, type SettingsSection } from './lib/vm-settings'
import type { OwnerInfo, Profile, VmRecord, VmDraft, DoctorReport, HardwareCapabilities } from '../shared/types'

const stateNames: Record<string, string> = { STOPPED: '已关机', STARTING: '启动中', RUNNING: '运行中', STOPPING: '关机中', PAUSED: '已暂停', FAILED: '故障' }
const networkNames = { 'restricted-lab': '隔离网络', offline: '离线', nat: 'NAT' }
const active = (vm: VmRecord): boolean => !['STOPPED', 'FAILED'].includes(vm.status.state)
const errorText = (error: unknown): string => error instanceof Error ? error.message.replace(/^Error invoking remote method '[^']+': Error: /, '') : String(error)

function Status({ vm }: { vm: VmRecord }) { return <Badge variant={vm.status.state === 'RUNNING' ? 'success' : vm.status.state === 'FAILED' ? 'destructive' : 'secondary'}><span className={`status-dot ${vm.status.state === 'RUNNING' ? 'online' : ''}`} />{stateNames[vm.status.state] ?? vm.status.state}</Badge> }

export default function App() {
  const [owner, setOwner] = useState<OwnerInfo | null>(null)
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [defaultProfile, setDefaultProfile] = useState('')
  const [vms, setVms] = useState<VmRecord[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [page, setPage] = useState<'vms' | 'settings'>('vms')
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [createSource, setCreateSource] = useState<'image' | 'iso'>('image')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [confirm, setConfirm] = useState<{ vm: VmRecord; action: 'reset' | 'delete' | 'force' } | null>(null)
  const [closing, setClosing] = useState(false)
  const [doctor, setDoctor] = useState<DoctorReport | null>(null)
  const [editing, setEditing] = useState<VmRecord | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const reportError = useCallback((message: string) => setError(errorText(message)), [])
  const refresh = useCallback(async () => {
    const result = await window.palette.request('vms.list') as { vms: VmRecord[] }
    setVms(result.vms)
    setUpdatedAt(new Date())
  }, [])
  const connect = useCallback(async () => {
    setLoading(true); setError('')
    try {
      setOwner(await window.palette.connect())
      const available = await window.palette.request('profiles') as { profiles: Profile[]; default_profile: string }
      setProfiles(available.profiles); setDefaultProfile(available.default_profile)
      await refresh()
    } catch (error) { setError(errorText(error)) }
    finally { setLoading(false) }
  }, [refresh])
  useEffect(() => { void connect(); return window.palette.onClose(() => setClosing(true)) }, [connect])
  useEffect(() => {
    if (!owner) return
    let inFlight = false
    const interval = setInterval(() => {
      if (inFlight || document.hidden) return
      inFlight = true
      void refresh().catch(error => setError(errorText(error))).finally(() => { inFlight = false })
    }, 2000)
    return () => clearInterval(interval)
  }, [owner, refresh])
  const vm = vms.find(item => item.vm_id === selected)
  const filtered = vms.filter(item => `${item.name} ${item.vm_id}`.toLowerCase().includes(search.toLowerCase()))
  function openCreate(source: 'image' | 'iso' = 'image'): void { setCreateSource(source); setCreating(true) }
  async function operate(vm: VmRecord, action: string): Promise<void> {
    if (action === 'settings') { setEditing(vm); return }
    if (['reset', 'delete', 'force'].includes(action)) { setConfirm({ vm, action: action as 'reset' | 'delete' | 'force' }); return }
    await execute(vm, action)
  }
  async function execute(vm: VmRecord, action: string): Promise<void> {
    setBusy(vm.vm_id); setError('')
    try {
      const method = action === 'force' ? 'vms.stop' : `vms.${action}`
      await window.palette.request(method as 'vms.start', vm.vm_id, action === 'force' ? { force: true } : {})
      if (action === 'delete') setSelected(null)
      setConfirm(null); await refresh()
    } catch (error) { setError(errorText(error)) }
    finally { setBusy(null) }
  }
  function actions(item: VmRecord) {
    return <NativeSelect aria-label={`${item.name} 的操作`} value="" disabled={busy === item.vm_id || ['STARTING', 'STOPPING'].includes(item.status.state)} onChange={event => { if (event.target.value) void operate(item, event.target.value) }}>
      <option value="">更多操作</option>
      {item.persistent && <option value="settings">虚拟机设置</option>}
      {active(item) && <><option value="stop">关机</option>{item.persistent && <option value="restart">重启</option>}<option value="force">强制停止</option></>}
      <option value="reset">恢复初始状态</option>
      {item.persistent && !active(item) && <option value="delete">删除虚拟机</option>}
    </NativeSelect>
  }
  return <div className={`app-shell ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
    <Sidebar className="app-sidebar">
      <SidebarHeader className="sidebar-brand"><ComputerIcon className="size-5" /><span className="brand-name">Palette</span><span className="local-label">本地</span></SidebarHeader>
      <SidebarContent><SidebarGroup className="primary-navigation"><SidebarItem disabled={!owner} icon={<PlusIcon className="size-4" />} onClick={() => openCreate()}>新建虚拟机</SidebarItem><SidebarItem icon={<ComputerIcon className="size-4" />} active={page === 'vms' && !selected} onClick={() => { setPage('vms'); setSelected(null) }}>全部虚拟机</SidebarItem></SidebarGroup>
        <SidebarGroup className="machine-navigation"><div className="sidebar-section-label"><span>虚拟机</span><span>{vms.length}</span></div>{vms.length ? vms.map(item => <SidebarItem key={item.vm_id} active={selected === item.vm_id && page === 'vms'} icon={<span className={`status-dot ${item.status.state === 'RUNNING' ? 'online' : ''}`} />} onClick={() => { setPage('vms'); setSelected(item.vm_id) }}>{item.name}</SidebarItem>) : <p className="sidebar-hint">创建的虚拟机会显示在这里</p>}</SidebarGroup>
      </SidebarContent>
      <SidebarFooter><SidebarItem icon={<SettingsIcon className="size-4" />} active={page === 'settings'} onClick={() => { setPage('settings'); setSelected(null) }}>环境设置</SidebarItem><div className="connection-status"><span className={`status-dot ${owner ? 'online' : ''}`} /><span>{owner ? owner.owner_kind === 'daemon' ? '后台已连接' : '由 MCP 会话托管' : '后台未连接'}</span></div></SidebarFooter>
    </Sidebar>
    <main className="workspace">
      <div className="window-drag-region" />
      <div className="workspace-topbar"><div className="workspace-location"><Button variant="ghost" size="icon" aria-label={sidebarCollapsed ? '展开侧栏' : '收起侧栏'} onClick={() => setSidebarCollapsed(value => !value)}><span className="sidebar-toggle-icon" /></Button><span>本地工作空间</span><span className="breadcrumb-divider">/</span><strong>{page === 'settings' ? '环境设置' : vm?.name ?? '虚拟机'}</strong></div><span className="workspace-indicator"><span className={`status-dot ${owner ? 'online' : ''}`} />{vms.filter(item => item.status.state === 'RUNNING').length} 台运行中</span></div>
      <div className={`workspace-content ${vm && page === 'vms' ? 'detail-workspace' : ''}`}>
      {error && <div className="error-banner" role="alert"><span>{error}</span><Button variant="ghost" size="sm" onClick={() => setError('')}>关闭</Button></div>}
      {loading ? <div className="loading-state" role="status">正在连接后台…</div> : page === 'settings' ? <>
        <header className="page-header"><div><h1>环境设置</h1><p>检查运行环境与后台连接。</p></div><Button onClick={() => void window.palette.request('doctor').then(data => setDoctor(data as DoctorReport)).catch(error => setError(errorText(error)))}>检查环境</Button></header>
        <Card><CardContent className="settings-content"><div><Label>配置目录</Label><p className="path-value">{owner?.config_root ?? '未连接'}</p><Button variant="secondary" onClick={() => void window.palette.chooseRoot().then(async result => { if (result) { setSelected(null); setDoctor(null); await connect() } }).catch(error => setError(errorText(error)))}><FolderIcon className="size-4" />选择目录</Button></div><div><Label>运行目录</Label><p className="path-value">{owner?.runtime_root ?? '未连接'}</p></div><div><Label>后台版本</Label><p>{owner?.version ?? '—'}</p><Button variant="outline" onClick={() => void connect()}>重新连接</Button></div></CardContent></Card>
        {doctor && <Card><CardContent className="settings-content"><h2>{doctor.ok ? '环境检查通过' : '环境需要配置'}</h2><Table><TableHeader><TableRow><TableHead>检查项</TableHead><TableHead>结果</TableHead><TableHead>详情</TableHead></TableRow></TableHeader><TableBody>{doctor.checks.map((check, index) => <TableRow key={index}><TableCell>{check.name}</TableCell><TableCell>{check.ok ? '通过' : check.blocking ? '需要处理' : '提示'}</TableCell><TableCell className="break-all text-muted-foreground">{typeof check.detail === 'string' ? check.detail : JSON.stringify(check.detail ?? '')}</TableCell></TableRow>)}</TableBody></Table></CardContent></Card>}
      </> : vm ? <>
        <header className="page-header"><div className="flex items-center gap-4"><Button variant="ghost" size="icon" aria-label="返回虚拟机列表" onClick={() => setSelected(null)}><ArrowLeftIcon className="size-5" /></Button><div><h1>{vm.name}</h1><div className="detail-meta"><Status vm={vm} /><span>{vm.status.guest_arch ?? profiles.find(profile => profile.profile_id === vm.definition?.draft.profile_id)?.qemu.arch}</span></div></div></div><div className="flex items-center gap-3">{vm.persistent && <Button variant="outline" onClick={() => setEditing(vm)}><SettingsIcon className="size-4" />虚拟机设置</Button>}{!active(vm) && <Button disabled={busy === vm.vm_id} onClick={() => void operate(vm, 'start')}><PlayIcon className="size-4" />{busy === vm.vm_id ? '启动中…' : '启动'}</Button>}{actions(vm)}</div></header>
        <Tabs defaultValue="console"><TabsList><TabsTrigger value="console">控制台</TabsTrigger><TabsTrigger value="status">状态</TabsTrigger><TabsTrigger value="configuration">配置</TabsTrigger></TabsList><TabsContent value="console"><Console key={vm.vm_id} vm={vm} reportError={reportError} /></TabsContent><TabsContent value="status"><LiveStatus vm={vm} updatedAt={updatedAt} /></TabsContent><TabsContent value="configuration"><Card><CardContent className="settings-content"><Configuration vm={vm} />{vm.persistent && !active(vm) && <Button variant="secondary" onClick={() => setEditing(vm)}>编辑配置</Button>}{vm.status.last_error && <p className="text-destructive">{vm.status.last_error.code}: {vm.status.last_error.message}</p>}</CardContent></Card></TabsContent></Tabs>
      </> : <>
        <header className="page-header overview-header"><div><span className="section-eyebrow">工作空间</span><h1>虚拟机</h1><p>你的系统、文件和正在进行的工作。</p></div><Button disabled={!owner} onClick={() => openCreate()}><PlusIcon className="size-4" />创建虚拟机</Button></header>
        {!owner ? <div className="empty-content"><ComputerIcon className="size-8 text-muted-foreground" /><h2>后台未连接</h2><Button onClick={() => void connect()}>重新连接</Button></div> : vms.length === 0 ? <div className="empty-content"><div className="empty-icon"><ComputerIcon className="size-7" /></div><h2>还没有虚拟机</h2><p>从一个熟悉的系统开始。</p><div className="quick-create-options"><Button variant="outline" onClick={() => openCreate('image')}><FolderIcon className="size-5" /><span><strong>导入磁盘镜像</strong><small>继续使用已有的系统</small></span><PlusIcon className="size-4" /></Button><Button variant="outline" onClick={() => openCreate('iso')}><ComputerIcon className="size-5" /><span><strong>从 ISO 安装</strong><small>创建一个全新的环境</small></span><PlusIcon className="size-4" /></Button></div><span className="empty-caption">支持 qcow2、raw 和 ISO 文件</span></div> : <div className="vm-table-content"><div className="list-tools"><div className="list-heading">全部虚拟机 <span>{vms.length}</span></div><div className="search-field"><SearchIcon className="size-4" /><Input aria-label="搜索虚拟机" placeholder="搜索名称" value={search} onChange={event => setSearch(event.target.value)} /></div></div><Table><TableHeader><TableRow><TableHead>名称</TableHead><TableHead>状态</TableHead><TableHead>资源</TableHead><TableHead>架构</TableHead><TableHead className="text-right">操作</TableHead></TableRow></TableHeader><TableBody>{filtered.map(item => <TableRow key={item.vm_id}><TableCell><Button variant="link" className="vm-name" onClick={() => setSelected(item.vm_id)}><span className="machine-icon"><ComputerIcon className="size-4" /></span>{item.name}</Button></TableCell><TableCell><Status vm={item} /></TableCell><TableCell className="text-muted-foreground">{item.definition ? `${item.definition.draft.vcpus} CPU / ${item.definition.draft.memory_mib / 1024} GiB` : '—'}</TableCell><TableCell className="text-muted-foreground">{item.status.guest_arch ?? profiles.find(profile => profile.profile_id === item.definition?.draft.profile_id)?.qemu.arch ?? '—'}</TableCell><TableCell><div className="row-actions"><Button size="sm" variant="ghost" disabled={busy === item.vm_id || ['STARTING', 'STOPPING'].includes(item.status.state)} onClick={() => setSelected(item.vm_id)}>打开</Button>{actions(item)}</div></TableCell></TableRow>)}{!filtered.length && <TableRow><TableCell colSpan={5} className="text-center text-muted-foreground">没有匹配的虚拟机</TableCell></TableRow>}</TableBody></Table></div>}
      </>}
      </div>
    </main>
    <CreateVm open={creating} source={createSource} onClose={() => setCreating(false)} profiles={profiles} defaultProfile={defaultProfile} advancedSupported={owner?.features.includes('vm-settings-v2') ?? false} onCreated={async id => { setCreating(false); setPage('vms'); await refresh(); setSelected(id) }} />
    <Dialog open={!!confirm} onOpenChange={open => { if (!open && !busy) setConfirm(null) }}><DialogContent><DialogHeader><DialogTitle>{confirm?.action === 'delete' ? '删除虚拟机' : confirm?.action === 'force' ? '强制停止虚拟机' : '恢复初始状态'}</DialogTitle><DialogDescription>{confirm?.action === 'delete' ? '删除定义和受管理磁盘。原始镜像不会被删除。' : confirm?.action === 'force' ? '立即停止运行，尚未保存的数据可能丢失。' : confirm?.vm.definition?.draft.source === 'iso' ? '清空当前磁盘，重新从 ISO 安装系统。' : '丢弃当前磁盘修改，重新使用初始镜像。'}</DialogDescription></DialogHeader>{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<DialogFooter><Button variant="secondary" disabled={!!busy} onClick={() => setConfirm(null)}>取消</Button><Button variant="destructive" disabled={!!busy} onClick={() => confirm && void execute(confirm.vm, confirm.action)}>{busy ? '处理中…' : '确认'}</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={closing} onOpenChange={open => { if (!open && !busy) setClosing(false) }}><DialogContent><DialogHeader><DialogTitle>虚拟机仍在运行</DialogTitle><DialogDescription>{owner?.owner_kind === 'embedded' ? '后台由 MCP 会话托管，MCP 会话结束时虚拟机会停止。' : '选择继续后台运行，或关机后退出。'}</DialogDescription></DialogHeader>{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<DialogFooter><Button variant="ghost" disabled={!!busy} onClick={() => { setClosing(false); void window.palette.finishClose('cancel') }}>取消</Button><Button variant="secondary" disabled={!!busy} onClick={() => void window.palette.finishClose('background').catch(error => setError(errorText(error)))}>后台继续运行</Button><Button disabled={!!busy} onClick={() => { setBusy('exit'); void window.palette.finishClose('shutdown').catch(error => setError(errorText(error))).finally(() => setBusy(null)) }}>{busy === 'exit' ? '正在关机…' : '全部关机并退出'}</Button></DialogFooter></DialogContent></Dialog>
    {editing && <VmSettings vm={vms.find(item => item.vm_id === editing.vm_id) ?? editing} owner={owner} onClose={() => setEditing(null)} onSaved={refresh} />}
  </div>
}

function LiveStatus({ vm, updatedAt }: { vm: VmRecord; updatedAt: Date | null }) {
  const status = vm.status
  const controlNames: Record<string, string> = { available: '可操作', held: '已接管', releasing: '正在释放', faulted: '释放未确认，暂停写入' }
  return <section className="live-status" aria-label="虚拟机实时状态"><div className="live-status-heading"><h2>运行状态</h2><span>每 2 秒更新{updatedAt && ` · ${updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}`}</span></div><dl className="configuration-grid"><div><dt>虚拟机标识</dt><dd className="font-mono">{vm.vm_id}</dd></div><div><dt>当前实例</dt><dd className="font-mono">{status.instance_id ?? '尚未启动'}</dd></div><div><dt>运行状态</dt><dd>{stateNames[status.state] ?? status.state}</dd></div><div><dt>控制权</dt><dd data-testid="control-owner">{status.instance_id ? status.control_owner === 'operator' ? '人工' : 'Agent' : '—'}{status.control_state && ` / ${controlNames[status.control_state] ?? status.control_state}`}</dd></div><div><dt>操作序号</dt><dd data-testid="action-seq" className="font-mono">{status.action_seq ?? 0}</dd></div><div><dt>排队任务</dt><dd>{status.queued_actions ?? 0}</dd></div><div><dt>管理通道</dt><dd>{status.qmp_ready ? '已连接' : '未就绪'}</dd></div><div><dt>画面</dt><dd>{status.screen_ready ? '已就绪' : '未就绪'}{status.display && ` / ${status.display.width} × ${status.display.height}`}</dd></div><div><dt>客户机桌面</dt><dd>{status.desktop_ready == null ? '尚未确认' : status.desktop_ready ? '已就绪' : '未就绪'}</dd></div><div><dt>代次（启动 / 显示 / 控制）</dt><dd className="font-mono">{status.boot_epoch ?? 0} / {status.display_epoch ?? 0} / {status.controller_epoch ?? 0}</dd></div><div><dt>保持的按键</dt><dd data-testid="held-keys">{status.input_ledger?.keys.join(', ') || '无'}</dd></div><div><dt>保持的鼠标按钮</dt><dd>{status.input_ledger?.buttons.join(', ') || '无'}</dd></div></dl>{status.input_ledger?.release_confirmed === false && <p role="alert" className="text-destructive">输入释放尚未确认</p>}{status.last_error && <p role="alert" className="text-destructive">{status.last_error.code}: {status.last_error.message}</p>}</section>
}

function Configuration({ vm }: { vm: VmRecord }) {
  const draft = vm.definition?.draft
  const hardware = draft?.settings
  const iso = hardware?.iso_path ?? (draft?.source === 'iso' ? draft.path : null)
  return <dl className="configuration-grid"><div><dt>标识</dt><dd className="font-mono">{vm.vm_id}</dd></div><div><dt>CPU</dt><dd>{draft?.vcpus ?? '—'} 核</dd></div><div><dt>内存</dt><dd>{draft ? draft.memory_mib / 1024 : '—'} GiB</dd></div><div><dt>磁盘容量</dt><dd>{draft?.disk_gib ?? '—'} GiB</dd></div><div><dt>网络</dt><dd>{draft ? networkNames[draft.network] : '—'}</dd></div><div><dt>磁盘模式</dt><dd>{vm.persistent ? '保留修改' : '临时桌面'}</dd></div>{hardware && <><div><dt>虚拟显卡</dt><dd>{hardware.display.model}</dd></div><div><dt>默认分辨率</dt><dd>{hardware.display.default_resolution ? `${hardware.display.width} × ${hardware.display.height}` : '由固件或系统决定'}</dd></div><div><dt>声音</dt><dd>{hardware.audio_enabled ? '启用' : '关闭'}</dd></div><div><dt>启动顺序</dt><dd>{{ auto: '自动', disk: '硬盘优先', cdrom: '光盘优先' }[hardware.boot_order]}</dd></div></>}{iso && <div className="col-span-2"><dt>安装介质 · {vm.definition?.iso_attached ? '已挂载' : '未挂载'}</dt><dd className="path-value">{iso}</dd></div>}{draft && <div className="col-span-2"><dt>原始来源</dt><dd className="path-value">{draft.path}</dd></div>}</dl>
}

function CreateVm({ open, source, onClose, profiles, defaultProfile, advancedSupported, onCreated }: { open: boolean; source: 'image' | 'iso'; onClose: () => void; profiles: Profile[]; defaultProfile: string; advancedSupported: boolean; onCreated: (id: string) => Promise<void> }) {
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
