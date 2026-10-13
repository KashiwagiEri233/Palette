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
import type { OwnerInfo, Profile, VmRecord, VmDraft, DoctorReport } from '../shared/types'

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
  const reportError = useCallback((message: string) => setError(errorText(message)), [])
  const refresh = useCallback(async () => {
    const result = await window.palette.request('vms.list') as { vms: VmRecord[] }
    setVms(result.vms)
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
        <header className="page-header"><div className="flex items-center gap-4"><Button variant="ghost" size="icon" aria-label="返回虚拟机列表" onClick={() => setSelected(null)}><ArrowLeftIcon className="size-5" /></Button><div><h1>{vm.name}</h1><div className="detail-meta"><Status vm={vm} /><span>{vm.status.guest_arch ?? profiles.find(profile => profile.profile_id === vm.definition?.draft.profile_id)?.qemu.arch}</span></div></div></div><div className="flex items-center gap-3">{!active(vm) && <Button disabled={busy === vm.vm_id} onClick={() => void operate(vm, 'start')}><PlayIcon className="size-4" />{busy === vm.vm_id ? '启动中…' : '启动'}</Button>}{actions(vm)}</div></header>
        <Tabs defaultValue="console"><TabsList><TabsTrigger value="console">控制台</TabsTrigger><TabsTrigger value="configuration">配置</TabsTrigger></TabsList><TabsContent value="console"><Console key={vm.vm_id} vm={vm} reportError={reportError} /></TabsContent><TabsContent value="configuration"><Card><CardContent className="settings-content"><Configuration vm={vm} />{vm.persistent && !active(vm) && <Button variant="secondary" onClick={() => setEditing(vm)}>编辑配置</Button>}{vm.status.last_error && <p className="text-destructive">{vm.status.last_error.code}: {vm.status.last_error.message}</p>}</CardContent></Card></TabsContent></Tabs>
      </> : <>
        <header className="page-header overview-header"><div><span className="section-eyebrow">工作空间</span><h1>虚拟机</h1><p>你的系统、文件和正在进行的工作。</p></div><Button disabled={!owner} onClick={() => openCreate()}><PlusIcon className="size-4" />创建虚拟机</Button></header>
        {!owner ? <div className="empty-content"><ComputerIcon className="size-8 text-muted-foreground" /><h2>后台未连接</h2><Button onClick={() => void connect()}>重新连接</Button></div> : vms.length === 0 ? <div className="empty-content"><div className="empty-icon"><ComputerIcon className="size-7" /></div><h2>还没有虚拟机</h2><p>从一个熟悉的系统开始。</p><div className="quick-create-options"><Button variant="outline" onClick={() => openCreate('image')}><FolderIcon className="size-5" /><span><strong>导入磁盘镜像</strong><small>继续使用已有的系统</small></span><PlusIcon className="size-4" /></Button><Button variant="outline" onClick={() => openCreate('iso')}><ComputerIcon className="size-5" /><span><strong>从 ISO 安装</strong><small>创建一个全新的环境</small></span><PlusIcon className="size-4" /></Button></div><span className="empty-caption">支持 qcow2、raw 和 ISO 文件</span></div> : <div className="vm-table-content"><div className="list-tools"><div className="list-heading">全部虚拟机 <span>{vms.length}</span></div><div className="search-field"><SearchIcon className="size-4" /><Input aria-label="搜索虚拟机" placeholder="搜索名称" value={search} onChange={event => setSearch(event.target.value)} /></div></div><Table><TableHeader><TableRow><TableHead>名称</TableHead><TableHead>状态</TableHead><TableHead>资源</TableHead><TableHead>架构</TableHead><TableHead className="text-right">操作</TableHead></TableRow></TableHeader><TableBody>{filtered.map(item => <TableRow key={item.vm_id}><TableCell><Button variant="link" className="vm-name" onClick={() => setSelected(item.vm_id)}><span className="machine-icon"><ComputerIcon className="size-4" /></span>{item.name}</Button></TableCell><TableCell><Status vm={item} /></TableCell><TableCell className="text-muted-foreground">{item.definition ? `${item.definition.draft.vcpus} CPU / ${item.definition.draft.memory_mib / 1024} GiB` : '—'}</TableCell><TableCell className="text-muted-foreground">{item.status.guest_arch ?? profiles.find(profile => profile.profile_id === item.definition?.draft.profile_id)?.qemu.arch ?? '—'}</TableCell><TableCell><div className="row-actions"><Button size="sm" variant="ghost" disabled={busy === item.vm_id || ['STARTING', 'STOPPING'].includes(item.status.state)} onClick={() => setSelected(item.vm_id)}>打开</Button>{actions(item)}</div></TableCell></TableRow>)}{!filtered.length && <TableRow><TableCell colSpan={5} className="text-center text-muted-foreground">没有匹配的虚拟机</TableCell></TableRow>}</TableBody></Table></div>}
      </>}
      </div>
    </main>
    <CreateVm open={creating} source={createSource} onClose={() => setCreating(false)} profiles={profiles} defaultProfile={defaultProfile} onCreated={async id => { setCreating(false); setPage('vms'); await refresh(); setSelected(id) }} />
    <Dialog open={!!confirm} onOpenChange={open => { if (!open && !busy) setConfirm(null) }}><DialogContent><DialogHeader><DialogTitle>{confirm?.action === 'delete' ? '删除虚拟机' : confirm?.action === 'force' ? '强制停止虚拟机' : '恢复初始状态'}</DialogTitle><DialogDescription>{confirm?.action === 'delete' ? '删除定义和受管理磁盘。原始镜像不会被删除。' : confirm?.action === 'force' ? '立即停止运行，尚未保存的数据可能丢失。' : confirm?.vm.definition?.draft.source === 'iso' ? '清空当前磁盘，重新从 ISO 安装系统。' : '丢弃当前磁盘修改，重新使用初始镜像。'}</DialogDescription></DialogHeader>{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<DialogFooter><Button variant="secondary" disabled={!!busy} onClick={() => setConfirm(null)}>取消</Button><Button variant="destructive" disabled={!!busy} onClick={() => confirm && void execute(confirm.vm, confirm.action)}>{busy ? '处理中…' : '确认'}</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={closing} onOpenChange={open => { if (!open && !busy) setClosing(false) }}><DialogContent><DialogHeader><DialogTitle>虚拟机仍在运行</DialogTitle><DialogDescription>{owner?.owner_kind === 'embedded' ? '后台由 MCP 会话托管，MCP 会话结束时虚拟机会停止。' : '选择继续后台运行，或关机后退出。'}</DialogDescription></DialogHeader>{error && <p role="alert" className="text-sm text-destructive">{error}</p>}<DialogFooter><Button variant="ghost" disabled={!!busy} onClick={() => { setClosing(false); void window.palette.finishClose('cancel') }}>取消</Button><Button variant="secondary" disabled={!!busy} onClick={() => void window.palette.finishClose('background').catch(error => setError(errorText(error)))}>后台继续运行</Button><Button disabled={!!busy} onClick={() => { setBusy('exit'); void window.palette.finishClose('shutdown').catch(error => setError(errorText(error))).finally(() => setBusy(null)) }}>{busy === 'exit' ? '正在关机…' : '全部关机并退出'}</Button></DialogFooter></DialogContent></Dialog>
    {editing && <EditVm vm={editing} onClose={() => setEditing(null)} onSaved={async () => { setEditing(null); await refresh() }} />}
  </div>
}

function Configuration({ vm }: { vm: VmRecord }) {
  const draft = vm.definition?.draft
  return <dl className="configuration-grid"><div><dt>标识</dt><dd className="font-mono">{vm.vm_id}</dd></div><div><dt>CPU</dt><dd>{draft?.vcpus ?? '—'} 核</dd></div><div><dt>内存</dt><dd>{draft ? draft.memory_mib / 1024 : '—'} GiB</dd></div><div><dt>磁盘容量</dt><dd>{draft?.disk_gib ?? '—'} GiB</dd></div><div><dt>网络</dt><dd>{draft ? networkNames[draft.network] : '—'}</dd></div><div><dt>磁盘模式</dt><dd>{vm.persistent ? '保留修改' : '临时桌面'}</dd></div>{draft && <div className="col-span-2"><dt>{draft.source === 'iso' ? '安装介质' : '基础镜像'}</dt><dd className="path-value">{draft.path}</dd></div>}</dl>
}

function CreateVm({ open, source, onClose, profiles, defaultProfile, onCreated }: { open: boolean; source: 'image' | 'iso'; onClose: () => void; profiles: Profile[]; defaultProfile: string; onCreated: (id: string) => Promise<void> }) {
  const [step, setStep] = useState(0)
  const [draft, setDraft] = useState<VmDraft>({ name: '', profile_id: '', source: 'image', path: '', image_format: 'qcow2', vcpus: 2, memory_mib: 4096, disk_gib: 24, network: 'restricted-lab' })
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => { if (open) { setStep(0); setError(''); const profile = profiles.find(item => item.profile_id === defaultProfile) ?? profiles[0]; setDraft({ name: '', profile_id: profile?.profile_id ?? '', source, path: '', image_format: 'qcow2', vcpus: profile?.resources.vcpus ?? 2, memory_mib: profile?.resources.memory_mib ?? 4096, disk_gib: profile?.images.size_gib ?? 24, network: 'restricted-lab' }) } }, [open, source])
  const change = <K extends keyof VmDraft>(key: K, value: VmDraft[K]): void => setDraft(previous => ({ ...previous, [key]: value }))
  async function create(): Promise<void> {
    setSaving(true); setError('')
    try { const result = await window.palette.request('vms.create', undefined, { ...draft }) as { vm_id: string }; await onCreated(result.vm_id) }
    catch (error) { setError(errorText(error)) }
    finally { setSaving(false) }
  }
  return <Dialog open={open} onOpenChange={value => { if (!value && !saving) onClose() }}><DialogContent className="create-dialog"><DialogHeader><DialogTitle>{['选择来源', '配置虚拟机', '确认创建'][step]}</DialogTitle><DialogDescription>{['选择已有磁盘镜像，或使用 ISO 安装系统。', '选择匹配系统架构的配置与资源。', '创建后可以启动虚拟机并打开控制台。'][step]}</DialogDescription></DialogHeader>
    <ol className="wizard-steps" aria-label="创建步骤">{['来源', '配置', '确认'].map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined} className={index <= step ? 'reached' : ''}><span>{index + 1}</span>{label}</li>)}</ol>
    {step === 0 ? <div className="form-stack"><div className="source-options"><Button variant={draft.source === 'image' ? 'default' : 'secondary'} onClick={() => { change('source', 'image'); change('path', '') }}>磁盘镜像</Button><Button variant={draft.source === 'iso' ? 'default' : 'secondary'} onClick={() => { change('source', 'iso'); change('path', '') }}>ISO 安装</Button></div><div><Label htmlFor="source-path">{draft.source === 'iso' ? 'ISO 文件' : '镜像文件'}</Label><div className="file-field"><Input id="source-path" value={draft.path} readOnly placeholder="选择文件" /><Button variant="secondary" onClick={() => void window.palette.chooseFile(draft.source).then(path => { if (path) change('path', path) }).catch(error => setError(errorText(error)))}><FolderIcon className="size-4" />浏览</Button></div></div>{draft.source === 'image' && <div><Label htmlFor="image-format">镜像格式</Label><NativeSelect id="image-format" value={draft.image_format} onChange={event => change('image_format', event.target.value as 'qcow2' | 'raw')}><option value="qcow2">qcow2</option><option value="raw">raw</option></NativeSelect></div>}</div> : step === 1 ? <div className="form-stack"><div><Label htmlFor="vm-name">名称</Label><Input id="vm-name" autoFocus maxLength={120} value={draft.name} onChange={event => change('name', event.target.value)} placeholder="例如：开发环境" /></div><div><Label htmlFor="vm-profile">系统配置</Label><NativeSelect id="vm-profile" value={draft.profile_id} onChange={event => change('profile_id', event.target.value)}>{profiles.map(profile => <option key={profile.profile_id} value={profile.profile_id}>{profile.profile_id} / {profile.qemu.arch} / {profile.qemu.accelerator}</option>)}</NativeSelect></div><ResourceFields draft={draft} change={change} /></div> : <dl className="configuration-grid"><div><dt>名称</dt><dd>{draft.name}</dd></div><div><dt>配置</dt><dd>{draft.profile_id}</dd></div><div><dt>CPU / 内存</dt><dd>{draft.vcpus} 核 / {draft.memory_mib / 1024} GiB</dd></div><div><dt>磁盘容量</dt><dd>{draft.disk_gib} GiB</dd></div><div><dt>网络</dt><dd>{networkNames[draft.network]}</dd></div><div><dt>磁盘模式</dt><dd>保留修改</dd></div><div className="col-span-2"><dt>来源</dt><dd className="path-value">{draft.path}</dd></div></dl>}
    {error && <p className="text-sm text-destructive" role="alert">{error}</p>}<DialogFooter><Button variant="secondary" disabled={saving} onClick={() => step ? setStep(step - 1) : onClose()}>{step ? '上一步' : '取消'}</Button>{step < 2 ? <Button disabled={step === 0 ? !draft.path : !draft.name.trim() || !draft.profile_id || !Number.isInteger(draft.vcpus) || draft.vcpus < 1 || draft.vcpus > 64 || draft.memory_mib < 256 || draft.disk_gib < 1} onClick={() => setStep(step + 1)}>下一步</Button> : <Button disabled={saving} onClick={() => void create()}>{saving ? '创建中…' : '创建虚拟机'}</Button>}</DialogFooter></DialogContent></Dialog>
}

function ResourceFields({ draft, change, disk = true }: { draft: VmDraft; change: <K extends keyof VmDraft>(key: K, value: VmDraft[K]) => void; disk?: boolean }) {
  return <><div className="resource-fields"><div><Label htmlFor="vcpus">CPU 核数</Label><Input id="vcpus" type="number" min={1} max={64} value={draft.vcpus} onChange={event => change('vcpus', Number(event.target.value))} /></div><div><Label htmlFor="memory">内存 / GiB</Label><Input id="memory" type="number" min={0.25} step={0.25} value={draft.memory_mib / 1024} onChange={event => change('memory_mib', Math.round(Number(event.target.value) * 1024))} /></div>{disk && <div><Label htmlFor="disk">磁盘 / GiB</Label><Input id="disk" type="number" min={1} max={4096} value={draft.disk_gib} onChange={event => change('disk_gib', Number(event.target.value))} /></div>}</div><div><Label htmlFor="network">网络</Label><NativeSelect id="network" value={draft.network} onChange={event => change('network', event.target.value as VmDraft['network'])}><option value="restricted-lab">隔离网络</option><option value="offline">离线</option><option value="nat">NAT</option></NativeSelect></div></>
}

function EditVm({ vm, onClose, onSaved }: { vm: VmRecord; onClose: () => void; onSaved: () => Promise<void> }) {
  const [draft, setDraft] = useState(vm.definition!.draft)
  const [isoAttached, setIsoAttached] = useState(vm.definition!.iso_attached)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const change = <K extends keyof VmDraft>(key: K, value: VmDraft[K]): void => setDraft(previous => ({ ...previous, [key]: value }))
  return <Dialog open onOpenChange={open => { if (!open && !saving) onClose() }}><DialogContent><DialogHeader><DialogTitle>编辑配置</DialogTitle><DialogDescription>修改在下次启动时生效。</DialogDescription></DialogHeader><div className="form-stack"><Label htmlFor="edit-name">名称</Label><Input id="edit-name" maxLength={120} value={draft.name} onChange={event => change('name', event.target.value)} /><ResourceFields draft={draft} change={change} disk={false} />{draft.source === 'iso' && <div><Label htmlFor="install-media">安装介质</Label><NativeSelect id="install-media" value={isoAttached ? 'attached' : 'ejected'} onChange={event => setIsoAttached(event.target.value === 'attached')}><option value="attached">挂载 ISO</option><option value="ejected">移除 ISO</option></NativeSelect></div>}</div>{error && <p role="alert" className="text-destructive">{error}</p>}<DialogFooter><Button variant="secondary" disabled={saving} onClick={onClose}>取消</Button><Button disabled={saving || !draft.name.trim()} onClick={() => { setSaving(true); void window.palette.request('vms.update', vm.vm_id, { name: draft.name, vcpus: draft.vcpus, memory_mib: draft.memory_mib, network: draft.network, iso_attached: isoAttached }).then(onSaved).catch(error => setError(errorText(error))).finally(() => setSaving(false)) }}>{saving ? '保存中…' : '保存'}</Button></DialogFooter></DialogContent></Dialog>
}
