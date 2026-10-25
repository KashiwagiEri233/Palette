import { useCallback, useEffect, useRef, useState } from 'react'
import { Home, Monitor, Settings2, Plus, Search, ArrowLeft, MessageSquare, FolderInput, Disc, Pin } from '@/components/icons/workbench'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { Sidebar, SidebarHeader, SidebarContent, SidebarItem } from '@/components/ui/sidebar'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { Command, CommandInput, CommandList, CommandEmpty, CommandGroup, CommandItem } from '@/components/ui/command'
import { Toaster } from '@/components/ui/sonner'
import { PromptInput } from '@/components/ui/ai-agent-input'
import { DesktopShell, useViewportWidth } from './components/DesktopShell'
import { ConversationNavigation } from './components/ConversationNavigation'
import { ConversationPage } from './components/ConversationPage'
import { SettingsPage, settingsSections, type SettingsCategory, type ThemePreference } from './components/SettingsPage'
import { IconButton, VmStatusBadge, VmActions, errorText, isShutdownFailure } from './components/workspace-primitives'
import { Console } from './Console'
import { VmSettings } from './VmSettings'
import { CreateVm, LiveStatus, Configuration } from './VmPages'
import { defaultLayout, layoutStorageKey, parseLayout, resolveLayout, type LayoutPreferences } from './lib/layout'
import { useConversations } from './lib/use-conversations'
import type { OwnerInfo, Profile, VmRecord, DesktopMethod, ShutdownTimeout } from '../shared/types'
import type { Conversation, ConversationPatch } from '../shared/workspace'
import { useAgentSettings } from './lib/use-agent'

interface Route { page: 'home' | 'vms' | 'settings' | 'conversation'; vmId?: string; conversationId?: string; category?: SettingsCategory }

export default function App() {
  const [owner, setOwner] = useState<OwnerInfo | null>(null)
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [defaultProfile, setDefaultProfile] = useState('')
  const [vms, setVms] = useState<VmRecord[]>([])
  const [history, setHistory] = useState<{ routes: Route[]; index: number }>({ routes: [{ page: 'home' }], index: 0 })
  const route = history.routes[history.index]
  const [targetVmId, setTargetVmId] = useState<string | undefined>()
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const pendingOperation = useRef<{ token: symbol; vmId: string; instanceId?: string; action: string } | null>(null)
  const mutating = useRef(false)
  const [creatingConversation, setCreatingConversation] = useState(false)
  const newConversationPending = useRef(false)
  const [creating, setCreating] = useState(false)
  const [createSource, setCreateSource] = useState<'image' | 'iso'>('image')
  const [confirm, setConfirm] = useState<{ vm: VmRecord; action: 'reset' | 'delete' | 'force'; resumeExit?: boolean } | null>(null)
  const [shutdownFailedVm, setShutdownFailedVm] = useState<VmRecord | null>(null)
  const [conversationDialog, setConversationDialog] = useState<{ kind: 'rename' | 'delete' | 'clear'; item: Conversation } | null>(null)
  const [conversationTitle, setConversationTitle] = useState('')
  const [dialogBusy, setDialogBusy] = useState(false)
  const [choosingVm, setChoosingVm] = useState(false)
  const [commandOpen, setCommandOpen] = useState(false)
  const [closing, setClosing] = useState(false)
  const [closeBusy, setCloseBusy] = useState<'background' | 'shutdown' | null>(null)
  const [closeError, setCloseError] = useState('')
  const [closeFailure, setCloseFailure] = useState<ShutdownTimeout | null>(null)
  const submittingClose = useRef(false)
  const [toolsNotice, setToolsNotice] = useState('')
  const [editing, setEditing] = useState<VmRecord | null>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const [homeDraft, setHomeDraft] = useState('')
  const [theme, setTheme] = useState<ThemePreference>(() => { try { const saved = localStorage.getItem('palette.theme.v1'); return saved === 'dark' || saved === 'light' ? saved : 'system' } catch { return 'system' } })
  const [preferences, setPreferences] = useState<LayoutPreferences>(() => { try { return parseLayout(localStorage.getItem(layoutStorageKey)) } catch { return { ...defaultLayout } } })
  const width = useViewportWidth()
  const layout = resolveLayout(width, preferences)
  const reportError = useCallback((error: unknown) => setError(errorText(error)), [])
  const conversations = useConversations(reportError)
  const { settings: agentSettings } = useAgentSettings(owner?.config_root)
  const pendingSend = useRef<{ id: string; text: string; requestId: string } | null>(null)
  const loadConversations = conversations.load
  const currentConversation = conversations.conversations.find(item => item.id === route.conversationId)
  const currentVm = vms.find(vm => vm.vm_id === (route.page === 'conversation' ? currentConversation?.vm_id : route.vmId))
  const connection = owner ? owner.owner_kind === 'daemon' ? '后台已连接' : '由 MCP 会话托管' : '后台未连接'

  const navigate = useCallback((next: Route) => {
    setHistory(previous => ({ routes: [...previous.routes.slice(0, previous.index + 1), next], index: previous.index + 1 }))
    setCommandOpen(false)
  }, [])
  const updatePreferences = useCallback((patch: Partial<LayoutPreferences>) => setPreferences(previous => parseLayout(JSON.stringify({ ...previous, ...patch }))), [])
  useEffect(() => { try { localStorage.setItem(layoutStorageKey, JSON.stringify(preferences)) } catch { /* Layout can still be used without persistence. */ } }, [preferences])
  useEffect(() => {
    const system = matchMedia('(prefers-color-scheme: dark)')
    const apply = () => { document.documentElement.classList.toggle('dark', theme === 'dark' || theme === 'system' && system.matches); document.documentElement.dataset.theme = theme }
    apply()
    system.addEventListener('change', apply)
    try { localStorage.setItem('palette.theme.v1', theme) } catch { /* Theme remains usable. */ }
    return () => system.removeEventListener('change', apply)
  }, [theme])
  const refresh = useCallback(async () => {
    const result = await window.palette.request('vms.list') as { vms: VmRecord[] }
    setVms(result.vms)
    setUpdatedAt(new Date())
  }, [])
  const connect = useCallback(async () => {
    setLoading(true); setError('')
    try {
      const connected = await window.palette.connect()
      const available = await window.palette.request('profiles') as { profiles: Profile[]; default_profile: string }
      setOwner(connected); setProfiles(available.profiles); setDefaultProfile(available.default_profile)
      await refresh()
      await loadConversations(connected.config_root).catch(reportError)
    } catch (error) { setOwner(null); reportError(error) }
    finally { setLoading(false) }
  }, [refresh, loadConversations, reportError])
  useEffect(() => { void connect(); return window.palette.onClose(() => setClosing(true)) }, [connect])
  useEffect(() => {
    if (!owner || closing) return
    let inFlight = false
    const timer = setInterval(() => {
      if (inFlight || document.hidden) return
      inFlight = true
      void refresh().catch(reportError).finally(() => { inFlight = false })
    }, 2000)
    return () => clearInterval(timer)
  }, [owner, closing, refresh, reportError])

  async function newConversation(vmId = targetVmId, draft = '') {
    if (newConversationPending.current) return
    if (!vmId) { setChoosingVm(true); return }
    newConversationPending.current = true; setCreatingConversation(true)
    try {
      const item = await conversations.create(vmId, draft)
      setChoosingVm(false); setTargetVmId(vmId); setHomeDraft('')
      navigate({ page: 'conversation', conversationId: item.id })
      if (draft.trim() && agentSettings?.credentialConfigured && agentSettings.configuration.modelId && (!agentSettings.configuration.vision || vms.find(vm => vm.vm_id === vmId)?.status.state === 'RUNNING')) await sendConversation(item, draft)
    } catch (error) { reportError(error) }
    finally { newConversationPending.current = false; setCreatingConversation(false) }
  }
  async function sendConversation(item: Conversation, text: string) {
    await conversations.flush()
    if (!pendingSend.current || pendingSend.current.id !== item.id || pendingSend.current.text !== text) pendingSend.current = { id: item.id, text, requestId: crypto.randomUUID() }
    await window.palette.request('agent.start', item.vm_id, { conversationId: item.id, text, requestId: pendingSend.current.requestId })
    pendingSend.current = null
    conversations.setDraft(item.id, '')
    await conversations.flush()
  }
  function selectConversation(item: Conversation) { setTargetVmId(item.vm_id); navigate({ page: 'conversation', conversationId: item.id }) }
  function openVm(vm: VmRecord) { setTargetVmId(vm.vm_id); navigate({ page: 'vms', vmId: vm.vm_id }) }
  function openCreate(source: 'image' | 'iso' = 'image') { setCreateSource(source); setCreating(true) }
  function editConversation(item: Conversation, kind: 'rename' | 'delete' | 'clear') { setConversationTitle(item.title); setConversationDialog({ item, kind }) }
  async function updateConversation(item: Conversation, patch: ConversationPatch) {
    try { await conversations.flush(); await conversations.update(item.id, patch); if (patch.archived && route.conversationId === item.id) navigate({ page: 'home' }) }
    catch (error) { reportError(error) }
  }
  async function operate(vm: VmRecord, action: string) {
    if (action === 'power') action = 'force'
    if (action === 'settings') { setEditing(vm); return }
    if (['force', 'reset', 'delete'].includes(action)) { setConfirm({ vm, action: action as 'force' | 'reset' | 'delete' }); return }
    await execute(vm, action)
  }
  async function execute(vm: VmRecord, action: string, resumeExit = false) {
    const previous = pendingOperation.current
    const escalating = action === 'force' && previous?.vmId === vm.vm_id && previous.instanceId === vm.status.instance_id && ['stop', 'restart'].includes(previous.action)
    if (mutating.current && !escalating) return
    const operation = { token: Symbol(), vmId: vm.vm_id, instanceId: vm.status.instance_id, action }
    pendingOperation.current = operation
    mutating.current = true; setBusy(vm.vm_id); setBusyAction(action); setError(''); setShutdownFailedVm(null)
    let continueExit = false
    try {
      const method = (action === 'force' ? 'vms.stop' : `vms.${action}`) as DesktopMethod
      const checkInstance = action === 'force' || owner?.features.includes('vm-stop-identity')
      const payload = action.startsWith('tools.') ? { revision: vm.definition!.revision, instance_id: ['STOPPED', 'FAILED'].includes(vm.status.state) ? null : vm.status.instance_id } : method === 'vms.stop' ? { force: action === 'force', ...(checkInstance && vm.status.instance_id ? { expected_instance_id: vm.status.instance_id } : {}) } : {}
      await window.palette.request(method, vm.vm_id, payload)
      if (pendingOperation.current !== operation) return
      if (action === 'tools.mount') setToolsNotice(vm.status.state === 'RUNNING' ? '工具光盘已装载。在 Windows 客户机中打开光驱，运行 Install.cmd 并确认管理员权限。完成后按安装程序提示重启客户机。' : '工具光盘已配置，将在下次启动时装载。在 Windows 客户机中打开光驱并运行 Install.cmd。')
      if (action === 'delete') { await conversations.load(); setTargetVmId(undefined); navigate({ page: 'home' }) }
      setConfirm(null); await refresh()
      continueExit = resumeExit
    } catch (error) {
      if (pendingOperation.current !== operation) return
      reportError(error)
      if (owner?.features.includes('vm-stop-identity') && ['stop', 'restart'].includes(action) && isShutdownFailure(error)) {
        setShutdownFailedVm(vm)
        setError('正常关机未完成。可在客户机内关机，或确认后强制停止。')
      }
      // A failed graceful shutdown restores the previous state in the backend.
      void refresh().catch(() => {})
    }
    finally { if (pendingOperation.current === operation) { pendingOperation.current = null; mutating.current = false; setBusy(null); setBusyAction(null) } }
    if (continueExit) { setClosing(true); await finishClose('shutdown') }
  }
  async function finishClose(choice: 'background' | 'shutdown' | 'cancel') {
    if (submittingClose.current) return
    submittingClose.current = true; setCloseError(''); setCloseFailure(null)
    if (choice !== 'cancel') setCloseBusy(choice)
    try {
      if (choice === 'shutdown' && mutating.current) throw new Error('虚拟机操作尚未完成，请等待操作完成后重试退出，或选择仅关闭窗口')
      if (choice !== 'cancel') await conversations.flush()
      const result = await window.palette.finishClose(choice)
      if (result?.shutdownTimeout) {
        setCloseFailure(result.shutdownTimeout)
        setCloseError(`虚拟机未完成正常关机：${result.shutdownTimeout.name}。可以重试，或确认后强制停止此虚拟机并继续退出。`)
        void refresh().catch(() => {})
      }
      if (choice === 'cancel') setClosing(false)
    }
    catch (error) {
      setCloseError(errorText(error))
      void refresh().catch(() => {})
    }
    finally { submittingClose.current = false; setCloseBusy(null) }
  }
  function confirmForceExit() {
    const vm = vms.find(vm => vm.vm_id === closeFailure?.vm_id)
    if (!vm || vm.status.instance_id !== closeFailure?.instance_id) { setCloseFailure(null); setCloseError('虚拟机状态已变化，请重新尝试完全退出'); return }
    setClosing(false); setCloseFailure(null); setError('')
    setConfirm({ vm, action: 'force', resumeExit: true })
  }
  async function chooseRoot() {
    try {
      await conversations.flush()
      const result = await window.palette.chooseRoot()
      if (result) { setHistory({ routes: [{ page: 'settings', category: 'runtime' }], index: 0 }); setTargetVmId(undefined); await connect() }
    } catch (error) { reportError(error) }
  }
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if (event.isComposing || !(event.ctrlKey || event.metaKey) || event.altKey) return
      const key = event.key.toLowerCase()
      if (!['n', 'k', 'b', 'j'].includes(key)) return
      if (document.querySelector('[role="dialog"]') && key !== 'k') return
      event.preventDefault(); event.stopPropagation()
      if (key === 'n') void newConversation(currentConversation?.vm_id ?? targetVmId)
      if (key === 'k') setCommandOpen(value => !value)
      if (key === 'b' && route.page !== 'vms') updatePreferences({ sidebarHidden: !preferences.sidebarHidden })
      if (key === 'j' && route.page === 'conversation') {
        if (width < 1100) window.dispatchEvent(new Event('palette-toggle-workspace'))
        else updatePreferences({ workspaceHidden: !preferences.workspaceHidden })
      }
    }
    window.addEventListener('keydown', shortcut, true)
    return () => window.removeEventListener('keydown', shortcut, true)
  })

  const visibleConversations = conversations.conversations.filter(item => !item.archived && vms.some(vm => vm.vm_id === item.vm_id))
  const shuttingDown = ['stop', 'restart'].includes(busyAction ?? '') ? vms.find(vm => vm.vm_id === busy && vm.status.instance_id === pendingOperation.current?.instanceId && ['RUNNING', 'PAUSED', 'STOPPING', 'FAILED'].includes(vm.status.state)) : undefined
  const confirmationBusy = !!busy && !(confirm?.action === 'force' && confirm.vm.vm_id === busy && shuttingDown)
  const quickCreate = <div className="home-quick-create"><Button variant="outline" disabled={!owner} onClick={() => openCreate('image')}><FolderInput size={16} />导入磁盘镜像</Button><Button variant="outline" disabled={!owner} onClick={() => openCreate('iso')}><Disc size={16} />从 ISO 安装</Button></div>
  const rail = <Sidebar variant="rail" className="global-nav-rail"><SidebarContent>{([{ page: 'home', label: '主页', icon: Home }, { page: 'vms', label: '虚拟机管理', icon: Monitor }, { page: 'settings', label: '设置', icon: Settings2 }] as const).map(item => <IconButton key={item.page} variant="navigation" label={item.label} aria-current={(route.page === item.page || item.page === 'home' && route.page === 'conversation') ? 'page' : undefined} className={(route.page === item.page || item.page === 'home' && route.page === 'conversation') ? 'nav-rail-active' : ''} onClick={() => navigate({ page: item.page })}><item.icon size={18} /></IconButton>)}</SidebarContent><IconButton label={connection} onClick={() => { if (!owner) void connect(); else navigate({ page: 'settings', category: 'runtime' }) }}><span className={`status-dot ${owner ? 'online' : ''}`} aria-hidden="true" /></IconButton></Sidebar>
  const navigation = route.page === 'vms' ? null : route.page === 'settings' ? <Sidebar variant="inset" className="app-sidebar settings-navigation"><SidebarHeader>设置</SidebarHeader><SidebarContent>{settingsSections.map(section => <SidebarItem key={section.id} title={section.label} active={(route.category ?? 'runtime') === section.id} onClick={() => navigate({ page: 'settings', category: section.id })}>{layout.compact ? section.label.slice(0, 1) : section.label}</SidebarItem>)}</SidebarContent></Sidebar> : <ConversationNavigation vms={vms} conversations={conversations.conversations} selectedId={route.conversationId} compact={layout.compact} connection={connection} busy={creatingConversation || !owner} onNew={id => void newConversation(id ?? currentConversation?.vm_id ?? targetVmId)} onSelect={selectConversation} onOpenVm={openVm} onVmAction={(vm, action) => void operate(vm, action)} onUpdate={(item, patch) => void updateConversation(item, patch)} onRename={item => editConversation(item, 'rename')} onDelete={item => editConversation(item, 'delete')} onSearch={() => setCommandOpen(true)} onManage={() => navigate({ page: 'vms' })} />

  return <TooltipProvider><DesktopShell vmId={currentVm?.vm_id} preferences={preferences} onPreferences={updatePreferences} rail={rail} navigation={navigation} onSearch={() => setCommandOpen(true)} canBack={history.index > 0} canForward={history.index < history.routes.length - 1} onBack={() => setHistory(previous => ({ ...previous, index: Math.max(0, previous.index - 1) }))} onForward={() => setHistory(previous => ({ ...previous, index: Math.min(previous.routes.length - 1, previous.index + 1) }))}>
    <Dialog open={!!toolsNotice} onOpenChange={value => { if (!value) setToolsNotice('') }}><DialogContent><DialogHeader><DialogTitle>安装增强工具与驱动</DialogTitle><DialogDescription>{toolsNotice}</DialogDescription></DialogHeader><DialogFooter><Button onClick={() => setToolsNotice('')}>知道了</Button></DialogFooter></DialogContent></Dialog>
    {shuttingDown && <div className="desktop-shutdown-status" role="status"><span>正在等待 {shuttingDown.name} 完成关机…</span><Button variant="outline" size="sm" onClick={() => setConfirm({ vm: shuttingDown, action: 'force' })}>强制停止…</Button></div>}
    {error && <div className="desktop-error" role="alert"><span>{error}</span>{shutdownFailedVm && <Button variant="outline" size="sm" disabled={!!busy} onClick={() => setConfirm({ vm: shutdownFailedVm, action: 'force' })}>强制停止…</Button>}<Button variant="ghost" size="sm" onClick={() => { setError(''); setShutdownFailedVm(null) }}>关闭</Button></div>}
    {loading ? <div className="loading-state" role="status">正在连接后台…</div> : route.page === 'settings' ? <SettingsPage key={owner?.config_root} category={route.category ?? 'runtime'} owner={owner} theme={theme} onTheme={setTheme} onReconnect={() => void connect()} onChooseRoot={() => void chooseRoot()} onError={reportError} /> : route.page === 'conversation' ? currentConversation ? <ConversationPage key={currentConversation.id} conversation={currentConversation} vm={currentVm} preferences={preferences} busy={busy === currentVm?.vm_id} onPreferences={updatePreferences} onDraft={conversations.setDraft} draftState={conversations.draftStates[currentConversation.id]} onRetrySave={() => void conversations.flush().catch(reportError)} onRename={() => editConversation(currentConversation, 'rename')} onDelete={() => editConversation(currentConversation, 'delete')} onClear={() => editConversation(currentConversation, 'clear')} onExport={() => { void conversations.flush().then(() => window.palette.request('conversations.export', currentConversation.vm_id, { id: currentConversation.id })).catch(reportError) }} onAction={(vm, action) => void operate(vm, action)} onAgentSettings={() => navigate({ page: 'settings', category: 'agent' })} onSend={text => sendConversation(currentConversation, text)} reportError={reportError} /> : <div className="empty-content"><h1>对话不存在</h1><Button onClick={() => navigate({ page: 'home' })}>返回主页</Button></div> : route.page === 'home' ? <div className="home-page page-scroll"><div className="home-content">
      <section className="home-task"><h1>想让 Agent 做什么？</h1><PromptInput value={homeDraft} onValueChange={setHomeDraft} placeholder={agentSettings?.credentialConfigured ? '描述你的任务…' : '描述任务，保存为新对话…'} sendLabel={agentSettings?.credentialConfigured && (!agentSettings.configuration.vision || vms.find(vm => vm.vm_id === targetVmId)?.status.state === 'RUNNING') ? '发送' : '保存为新对话'} onSend={() => void newConversation(targetVmId, homeDraft)} sendDisabled={!targetVmId || !owner || creatingConversation} footer={<><Badge variant="secondary">{vms.find(vm => vm.vm_id === targetVmId)?.name ?? '选择目标虚拟机'}</Badge><span className="home-draft-label">{agentSettings?.credentialConfigured ? agentSettings.configuration.modelId : '保存任务草稿'}</span></>} /><div className="target-vm-chips">{vms.map(vm => <Button key={vm.vm_id} size="sm" variant={targetVmId === vm.vm_id ? 'secondary' : 'ghost'} aria-pressed={targetVmId === vm.vm_id} onClick={() => setTargetVmId(vm.vm_id)}><span className={`status-dot ${vm.status.state === 'RUNNING' ? 'online' : ''}`} />{vm.name}</Button>)}</div></section>
      {!owner ? <section className="home-empty"><h2>后台未连接</h2><Button onClick={() => void connect()}>重新连接</Button></section> : !vms.length ? <section className="home-empty"><Monitor size={28} /><h2>还没有虚拟机</h2>{quickCreate}<Button variant="ghost" onClick={() => openCreate()}>创建虚拟机</Button></section> : <><section className="home-section"><div className="section-heading"><h2>运行中的虚拟机</h2><Button variant="ghost" size="sm" onClick={() => navigate({ page: 'vms' })}>全部虚拟机</Button></div>{vms.filter(vm => vm.status.state === 'RUNNING').length ? <div className="running-vms">{vms.filter(vm => vm.status.state === 'RUNNING').map(vm => <Card key={vm.vm_id} className="running-vm-card"><CardContent><Monitor size={22} /><div><strong>{vm.name}</strong><VmStatusBadge vm={vm} /><small>{vm.definition ? `${vm.definition.draft.vcpus} 核 / ${vm.definition.draft.memory_mib / 1024} GiB（已配置）` : '资源信息不可用'}</small></div><Button variant="outline" size="sm" onClick={() => openVm(vm)}>打开</Button></CardContent></Card>)}</div> : <p className="section-empty">没有运行中的虚拟机</p>}</section><section className="home-section"><h2>最近会话</h2>{visibleConversations.length ? <div className="home-recent">{[...visibleConversations].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, 5).map(item => <Button key={item.id} variant="ghost" onClick={() => selectConversation(item)}><MessageSquare size={16} /><span>{item.title}</span><small>{vms.find(vm => vm.vm_id === item.vm_id)?.name}</small></Button>)}</div> : <p className="section-empty">暂无最近对话</p>}</section>{quickCreate}</>}
    </div></div> : currentVm ? <div className="vm-detail-page page-scroll"><header className="page-header"><div className="detail-identity"><IconButton label="返回虚拟机列表" onClick={() => navigate({ page: 'vms' })}><ArrowLeft size={18} /></IconButton><h1>{currentVm.name}</h1><VmStatusBadge vm={currentVm} /></div><div className="detail-actions">{currentVm.persistent && <IconButton label="虚拟机设置" onClick={() => setEditing(currentVm)}><Settings2 size={16} /></IconButton>}<Button variant="outline" onClick={() => void newConversation(currentVm.vm_id)}><Plus size={16} />新对话</Button><VmActions vm={currentVm} busy={busy === currentVm.vm_id} onAction={(vm, action) => void operate(vm, action)} /></div></header><Tabs defaultValue="console"><TabsList><TabsTrigger value="console">控制台</TabsTrigger><TabsTrigger value="status">状态</TabsTrigger><TabsTrigger value="configuration">配置</TabsTrigger></TabsList><TabsContent value="console"><Console key={currentVm.vm_id} vm={currentVm} reportError={reportError} onAction={action => void operate(currentVm, action)} busy={busy === currentVm.vm_id} /></TabsContent><TabsContent value="status"><LiveStatus vm={currentVm} updatedAt={updatedAt} /></TabsContent><TabsContent value="configuration"><Card><CardContent className="settings-content"><Configuration vm={currentVm} />{currentVm.persistent && <Button variant="outline" onClick={() => setEditing(currentVm)}>编辑配置</Button>}</CardContent></Card></TabsContent></Tabs></div> : <div className="vm-management-page page-scroll"><header className="page-header"><h1>虚拟机</h1><Button disabled={!owner} onClick={() => openCreate()}><Plus size={16} />创建虚拟机</Button></header><div className="management-search"><Search size={16} /><Input aria-label="搜索虚拟机" placeholder="搜索名称或标识" value={search} onChange={event => setSearch(event.target.value)} /></div>{!vms.length ? <div className="home-empty"><h2>还没有虚拟机</h2>{quickCreate}</div> : <Table><TableHeader><TableRow><TableHead>名称</TableHead><TableHead>状态</TableHead><TableHead>资源</TableHead><TableHead className="optional-column">架构</TableHead><TableHead className="optional-column">会话数</TableHead><TableHead className="text-right">操作</TableHead></TableRow></TableHeader><TableBody>{vms.filter(vm => `${vm.name} ${vm.vm_id}`.toLowerCase().includes(search.toLowerCase())).map(vm => <TableRow key={vm.vm_id}><TableCell><Button variant="link" className="vm-name" onClick={() => openVm(vm)}><Monitor size={16} />{vm.name}</Button></TableCell><TableCell><VmStatusBadge vm={vm} /></TableCell><TableCell>{vm.definition ? `${vm.definition.draft.vcpus} CPU / ${vm.definition.draft.memory_mib / 1024} GiB` : '—'}</TableCell><TableCell className="optional-column">{vm.status.guest_arch ?? profiles.find(profile => profile.profile_id === vm.definition?.draft.profile_id)?.qemu.arch ?? '—'}</TableCell><TableCell className="optional-column">{conversations.conversations.filter(item => item.vm_id === vm.vm_id).length}</TableCell><TableCell><div className="row-actions"><Button size="sm" variant="ghost" onClick={() => openVm(vm)}>打开</Button><VmActions vm={vm} busy={busy === vm.vm_id} compact inline onAction={(vm, action) => void operate(vm, action)} /></div></TableCell></TableRow>)}{!vms.some(vm => `${vm.name} ${vm.vm_id}`.toLowerCase().includes(search.toLowerCase())) && <TableRow><TableCell colSpan={6}>没有匹配的虚拟机</TableCell></TableRow>}</TableBody></Table>}</div>}
  </DesktopShell>
  <CreateVm open={creating} source={createSource} onClose={() => setCreating(false)} profiles={profiles} defaultProfile={defaultProfile} onCreated={async id => { setCreating(false); await refresh(); setTargetVmId(id); navigate({ page: 'vms', vmId: id }) }} />
  {editing && <VmSettings vm={vms.find(vm => vm.vm_id === editing.vm_id) ?? editing} owner={owner} onClose={() => setEditing(null)} onSaved={refresh} />}
  <Dialog open={!!confirm} onOpenChange={open => { if (!open && !confirmationBusy) setConfirm(null) }}><DialogContent closeDisabled={confirmationBusy}><DialogHeader><DialogTitle>{confirm?.action === 'delete' ? '删除虚拟机' : confirm?.action === 'force' ? '强制停止虚拟机' : '恢复初始状态'}</DialogTitle><DialogDescription>{confirm?.vm.name}：{confirm?.action === 'delete' ? '删除定义、受管理磁盘和全部对话。原始镜像不会被删除。' : confirm?.action === 'force' ? `这相当于强制断电，尚未保存的数据可能丢失，并可能损坏客户机文件系统。建议先在客户机操作系统内正常关机。${confirm.resumeExit ? '确认后将继续正常关闭其他虚拟机并退出 Palette。' : ''}` : '丢弃系统磁盘修改并恢复初始状态，附加镜像文件保留内容。'}</DialogDescription></DialogHeader>{error && <p role="alert">{error}</p>}<DialogFooter><Button variant="secondary" disabled={confirmationBusy} onClick={() => setConfirm(null)}>取消</Button><Button variant="destructive" disabled={confirmationBusy} onClick={() => confirm && void execute(confirm.vm, confirm.action, confirm.resumeExit)}>{confirmationBusy ? '处理中…' : '确认'}</Button></DialogFooter></DialogContent></Dialog>
  <Dialog open={!!conversationDialog} onOpenChange={open => { if (!open && !dialogBusy) setConversationDialog(null) }}><DialogContent closeDisabled={dialogBusy}><DialogHeader><DialogTitle>{conversationDialog?.kind === 'rename' ? '重命名对话' : conversationDialog?.kind === 'clear' ? '清空草稿？' : '删除对话？'}</DialogTitle><DialogDescription>{conversationDialog?.kind === 'rename' ? '为这项任务设置一个标题。' : '此操作无法撤销，虚拟机与磁盘不受影响。'}</DialogDescription></DialogHeader>{conversationDialog?.kind === 'rename' && <Input autoFocus aria-label="对话标题" value={conversationTitle} maxLength={120} onChange={event => setConversationTitle(event.target.value)} onKeyDown={event => { if (event.key === 'Escape') setConversationDialog(null) }} />}<DialogFooter><Button variant="secondary" disabled={dialogBusy} onClick={() => setConversationDialog(null)}>取消</Button><Button disabled={dialogBusy || conversationDialog?.kind === 'rename' && !conversationTitle.trim()} variant={conversationDialog?.kind === 'rename' ? 'default' : 'destructive'} onClick={() => {
    const selected = conversationDialog
    if (!selected) return
    setDialogBusy(true)
    void (async () => { await conversations.flush(); if (selected.kind === 'delete') { await conversations.remove(selected.item.id); if (route.conversationId === selected.item.id) navigate({ page: 'home' }) } else if (selected.kind === 'clear') { conversations.setDraft(selected.item.id, ''); await conversations.flush() } else await conversations.update(selected.item.id, { title: conversationTitle }); setConversationDialog(null) })().catch(reportError).finally(() => setDialogBusy(false))
  }}>{dialogBusy ? '正在保存…' : conversationDialog?.kind === 'rename' ? '保存' : '确认'}</Button></DialogFooter></DialogContent></Dialog>
  <Dialog open={choosingVm} onOpenChange={setChoosingVm}><DialogContent><DialogHeader><DialogTitle>选择目标虚拟机</DialogTitle><DialogDescription>每个对话属于一台虚拟机。</DialogDescription></DialogHeader>{vms.map(vm => <Button key={vm.vm_id} variant="outline" disabled={creatingConversation} onClick={() => void newConversation(vm.vm_id, homeDraft)}><Monitor size={16} />{vm.name}<VmStatusBadge vm={vm} /></Button>)}{!vms.length && <Button disabled={!owner} onClick={() => { setChoosingVm(false); openCreate() }}>创建虚拟机</Button>}</DialogContent></Dialog>
  <Dialog open={commandOpen} onOpenChange={setCommandOpen}><DialogContent className="command-dialog"><DialogTitle className="sr-only">命令面板</DialogTitle><Command><CommandInput aria-label="搜索虚拟机和会话" placeholder="搜索虚拟机、会话或操作…" /><CommandList><CommandEmpty>没有找到匹配结果</CommandEmpty><CommandGroup heading="操作"><CommandItem onSelect={() => { setCommandOpen(false); void newConversation() }}>新对话</CommandItem><CommandItem onSelect={() => navigate({ page: 'vms' })}>虚拟机管理</CommandItem><CommandItem onSelect={() => navigate({ page: 'settings' })}>环境设置</CommandItem></CommandGroup><CommandGroup heading="虚拟机">{vms.map(vm => <CommandItem key={vm.vm_id} value={`vm ${vm.name} ${vm.vm_id}`} onSelect={() => openVm(vm)}><Monitor size={16} />{vm.name}</CommandItem>)}</CommandGroup><CommandGroup heading="对话">{conversations.conversations.map(item => <CommandItem key={item.id} value={`conversation ${item.id} ${item.title} ${vms.find(vm => vm.vm_id === item.vm_id)?.name ?? ''}`} onSelect={() => selectConversation(item)}>{item.pinned ? <Pin size={14} /> : <MessageSquare size={14} />}{item.title}<small>{vms.find(vm => vm.vm_id === item.vm_id)?.name}{item.archived ? '（已归档）' : ''}</small></CommandItem>)}</CommandGroup></CommandList></Command></DialogContent></Dialog>
  <Dialog open={closing} onOpenChange={(open, details) => { if (!open) { if (submittingClose.current) details.cancel(); else void finishClose('cancel') } }}><DialogContent closeDisabled={!!closeBusy}><DialogHeader><DialogTitle>退出 Palette？</DialogTitle><DialogDescription>仅关闭窗口会保留后台和运行中的虚拟机。完全退出会先正常关闭所有虚拟机，再停止当前连接的后台。{owner?.owner_kind === 'embedded' && '后台由 MCP 会话托管，完全退出将结束托管服务并中断相关 MCP 连接。'}</DialogDescription></DialogHeader>{closeError && <p role="alert">{closeError}</p>}{closeFailure && <Button variant="destructive" disabled={!!closeBusy} onClick={confirmForceExit}>强制停止此虚拟机并退出…</Button>}<DialogFooter><Button variant="ghost" disabled={!!closeBusy} onClick={() => void finishClose('cancel')}>取消</Button><Button variant="secondary" disabled={!!closeBusy} onClick={() => void finishClose('background')}>{closeBusy === 'background' ? '正在关闭…' : '仅关闭窗口'}</Button><Button disabled={!!closeBusy} onClick={() => void finishClose('shutdown')}>{closeBusy === 'shutdown' ? '正在退出…' : '完全退出'}</Button></DialogFooter></DialogContent></Dialog>
  <Toaster theme={theme} position="bottom-right" />
  </TooltipProvider>
}
