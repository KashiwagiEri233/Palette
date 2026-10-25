import { app, BrowserWindow, dialog, ipcMain, protocol, net, Menu, safeStorage, shell, clipboard } from 'electron'
import { spawn } from 'node:child_process'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, join, resolve, relative, isAbsolute } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { call } from './ipc'
import { RuntimeShutdown, VmShutdownTimeout } from './shutdown'
import type { DesktopMethod, OwnerInfo, VmRecord } from '../shared/types'
import { workspaceMethods } from '../shared/workspace'
import { ConversationRepository } from './conversations'
import { agentMethods } from '../shared/agent'
import { PiAgentService } from './pi-agent'
import { externalLink } from '../shared/external-link'
import { checkedClipboard, checkedGuestPath, withGuestLease, uploadGuestFile, downloadGuestFile } from './guest-enhancements'
import type { GuestAction } from '../shared/guest'

const here = dirname(fileURLToPath(import.meta.url))
const backendRoot = resolve(here, '../../..')
let configRoot = process.env.PALETTE_ROOT ?? backendRoot
let runtimeRoot = join(configRoot, '.runtime')
let window: BrowserWindow
let allowedClose = false
let handlingClose = false
let connectionsInFlight = 0
const shutdown = new RuntimeShutdown()
const leases = new Map<string, string>()
const guestActions = new Map<string, AbortController>()
const repositories = new Map<string, ConversationRepository>()
const agentServices = new Map<string, PiAgentService>()
function agent(): PiAgentService {
  const root = resolve(configRoot)
  let service = agentServices.get(root)
  if (!service) {
    const repository = conversations()
    service = new PiAgentService(app.getPath('userData'), root, () => runtimeRoot, {
      available: () => safeStorage.isEncryptionAvailable() && (process.platform !== 'linux' || safeStorage.getSelectedStorageBackend() !== 'basic_text'),
      encrypt: value => safeStorage.encryptString(value), decrypt: value => safeStorage.decryptString(value)
    }, () => repository.list(), event => { if (!window.isDestroyed()) window.webContents.send('agent-event', event) })
    agentServices.set(root, service)
  }
  return service
}
async function stopAgents(): Promise<void> {
  for (const service of agentServices.values()) await service.close()
  agentServices.clear()
}
function conversations(): ConversationRepository {
  const root = resolve(configRoot)
  let repository = repositories.get(root)
  if (!repository) { repository = new ConversationRepository(app.getPath('userData'), root); repositories.set(root, repository) }
  return repository
}
const methods = new Set<DesktopMethod>(['profiles', 'doctor', 'vms.list', 'vms.create', 'vms.tools.mount', 'vms.tools.eject', 'vms.update', 'vms.start', 'vms.stop', 'vms.restart', 'vms.reset', 'vms.delete', 'display.ticket', 'display.sync', 'control.acquire', 'control.input', 'control.heartbeat', 'control.release'])
methods.add('guest.status')
methods.add('control.display.resize')
methods.add('image.info')
methods.add('os_presets.list')
methods.add('hardware.capabilities')
methods.add('vms.settings.get')
methods.add('vms.disk.resize')
methods.add('vms.disk.delete')
protocol.registerSchemesAsPrivileged([{ scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true } }])

function run(args: string[]): Promise<string> {
  return new Promise((resolveResult, reject) => {
    const child = spawn(process.env.PALETTE_UV ?? 'uv', ['run', '--project', backendRoot, 'python', '-m', 'palette.cli', '--root', configRoot, ...args], { cwd: backendRoot, windowsHide: true, shell: false })
    let stdout = '', stderr = ''
    child.stdout.on('data', data => { stdout += String(data); if (stdout.length > 2 * 1024 * 1024) child.kill() })
    child.stderr.on('data', data => { stderr = (stderr + String(data)).slice(-8192) })
    child.on('error', reject)
    child.on('exit', code => code === 0 ? resolveResult(stdout) : reject(new Error(stderr.trim() || '无法启动 Python 后台')))
  })
}

async function connectOwner(): Promise<OwnerInfo> {
  if (handlingClose) throw new Error('正在退出，请稍候')
  connectionsInFlight++
  try {
    const config = JSON.parse(await run(['config', 'show']))
    runtimeRoot = config.runtime_root
    try { await call(runtimeRoot, 'hello') }
    catch (error) {
      const message = String(error)
      if (!/ENOENT|ECONNREFUSED/.test(message)) throw error
      await run(['daemon', 'start'])
    }
    const result = await call(runtimeRoot, 'hello')
    if (result.desktop_protocol !== 1) throw new Error('后台协议版本不兼容')
    // Agent credentials never cross the preload boundary.
    return { owner_kind: result.owner_kind, config_root: result.config_root, runtime_root: result.runtime_root, version: result.version, features: Array.isArray(result.features) ? result.features : [] } as OwnerInfo
  } finally { connectionsInFlight-- }
}

async function releaseAll(): Promise<void> {
  await Promise.all([...leases].map(async ([vmId, leaseId]) => {
    try { const result = await call(runtimeRoot, 'control.release', vmId, { lease_id: leaseId }); if (result.release_confirmed === true) leases.delete(vmId) } catch { /* The backend lease still expires independently. */ }
  }))
}

function trusted(event: Electron.IpcMainInvokeEvent): void {
  const url = event.senderFrame?.url ?? ''
  const origin = process.env.ELECTRON_RENDERER_URL
  if (event.sender !== window.webContents || (origin ? !url.startsWith(origin + '/') && url !== origin : !url.startsWith('app://palette/'))) throw new Error('不允许的界面连接')
}

app.whenReady().then(async () => {
  if (!process.env.PALETTE_ROOT) {
    try { const settings = JSON.parse(await readFile(join(app.getPath('userData'), 'settings.json'), 'utf8')); if (typeof settings.configRoot === 'string') configRoot = settings.configRoot } catch { /* First launch uses this checkout. */ }
  }
  const rendererRoot = resolve(here, '../renderer')
  protocol.handle('app', request => {
    const url = new URL(request.url)
    const path = resolve(rendererRoot, decodeURIComponent(url.pathname).replace(/^\/+/, '') || 'index.html')
    const local = relative(rendererRoot, path)
    if (url.hostname !== 'palette' || local.startsWith('..') || isAbsolute(local)) return new Response('Not found', { status: 404 })
    return net.fetch(pathToFileURL(path).toString())
  })
  window = new BrowserWindow({
    width: 1440, height: 900, minWidth: 1100, minHeight: 700,
    title: 'Palette', backgroundColor: '#f7f6f5',
    titleBarStyle: process.platform === 'win32' ? 'hidden' : process.platform === 'darwin' ? 'hiddenInset' : 'default',
    ...(process.platform === 'darwin' ? { trafficLightPosition: { x: 12, y: 17 } } : {}),
    ...(process.platform === 'win32' ? { titleBarOverlay: { color: '#ffffff', symbolColor: '#252525', height: 48 } } : {}),
    webPreferences: { preload: join(here, '../preload/index.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true }
  })
  if (process.platform === 'win32') window.removeMenu()
  window.webContents.session.setPermissionRequestHandler((contents, permission, callback) => callback(permission === 'fullscreen' && contents === window.webContents))
  window.webContents.session.webRequest.onHeadersReceived((details, callback) => callback({ responseHeaders: { ...details.responseHeaders, 'Content-Security-Policy': [`default-src 'self'; script-src 'self'${process.env.ELECTRON_RENDERER_URL ? " 'unsafe-inline'" : ''}; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data: blob:; connect-src 'self' ws://127.0.0.1:*${process.env.ELECTRON_RENDERER_URL ? ' ws://localhost:5173' : ''}; object-src 'none'; base-uri 'none'`] } }))
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', event => event.preventDefault())
  window.webContents.on('leave-html-full-screen', () => {
    // Escape can end HTML fullscreen while the native window remains fullscreen.
    if (window.isFullScreen()) window.setFullScreen(false)
  })
  window.on('blur', () => { for (const action of guestActions.values()) action.abort(); void releaseAll(); window.webContents.send('control-lost') })
  window.webContents.on('render-process-gone', () => { void releaseAll() })
  window.on('close', event => {
    if (allowedClose) return
    event.preventDefault()
    if (handlingClose) return
    for (const action of guestActions.values()) action.abort()
    window.webContents.send('close-request')
    void releaseAll()
  })
  ipcMain.handle('connect', event => { trusted(event); if (handlingClose) throw new Error('正在退出，请稍候'); return connectOwner() })
  ipcMain.handle('window-backdrop', (event, opacity: unknown, background: unknown = '#f7f6f5') => {
    trusted(event)
    if (typeof opacity !== 'number' || !Number.isFinite(opacity) || opacity < 0 || opacity > 1) throw new Error('窗口遮罩参数无效')
    if (typeof background !== 'string' || !/^#[a-f0-9]{6}$/i.test(background)) throw new Error('窗口背景参数无效')
    if (process.platform !== 'win32') return
    const channels = [1, 3, 5].map(offset => Math.round(parseInt(background.slice(offset, offset + 2), 16) * (1 - opacity)))
    const color = `#${channels.map(channel => channel.toString(16).padStart(2, '0')).join('')}`
    window.setTitleBarOverlay({ color, symbolColor: channels.reduce((a, b) => a + b, 0) < 384 ? '#e5e5e5' : '#252525' })
  })
  ipcMain.handle('request', async (event, method: DesktopMethod, vmId?: string, payload: Record<string, unknown> = {}) => {
    trusted(event)
    if ((!methods.has(method) && !workspaceMethods.has(method) && !agentMethods.has(method)) || (vmId !== undefined && !/^[A-Za-z0-9_.-]{1,128}$/.test(vmId)) || typeof payload !== 'object' || payload === null || Array.isArray(payload) || Buffer.byteLength(JSON.stringify(payload)) > (String(method) === 'agent.providers.save' ? 1024 * 1024 : 64 * 1024)) throw new Error('请求无效')
    if (handlingClose) throw new Error('正在退出，请稍候')
    const operation = String(method)
    if (agentMethods.has(operation)) {
      const service = agent()
      if (operation === 'agent.settings.get') return service.settings()
      if (operation === 'agent.settings.update') return service.updateSettings(payload.configuration)
      if (operation === 'agent.providers.save') return service.saveProvider(payload.provider, payload.key as string | undefined)
      if (operation === 'agent.providers.delete') return service.deleteProvider(payload.providerId as string)
      if (operation === 'agent.models.discover') return service.discoverModels(payload as unknown as import('../shared/agent').AgentModelDiscovery)
      if (operation === 'agent.credentials.set' || operation === 'agent.credentials.remove') return service.configureCredential(payload.providerId as string, operation.endsWith('.set') ? payload.key as string : null)
      if (!vmId || typeof payload.conversationId !== 'string') throw new Error('会话参数无效')
      if (operation === 'agent.snapshot') return service.snapshot(vmId, payload.conversationId)
      if (operation === 'agent.start') return service.start(vmId, payload.conversationId, payload.text as string, payload.requestId as string)
      if (operation === 'agent.stop') return service.stop(vmId, payload.conversationId)
      if (operation === 'agent.compact') return service.compact(vmId, payload.conversationId)
      if (operation === 'agent.tree.navigate') return service.navigate(vmId, payload.conversationId, payload.entryId as string)
      throw new Error('不支持的 Agent 操作')
    }
    if (operation === 'window.command') {
      if (payload.action === 'close') window.close()
      else if (payload.action === 'open_external') {
        const url = externalLink(payload.url)
        if (!url) throw new Error('链接地址无效')
        await shell.openExternal(url)
      }
      else if (payload.action === 'menu') {
        const root = runtimeRoot
        let selected: VmRecord | undefined
        if (vmId) {
          try {
            const listed = await call(root, 'vms.list') as { vms: VmRecord[] }
            selected = listed.vms.find(vm => vm.vm_id === vmId)
          } catch { /* File/edit/view menus remain available while the backend is offline. */ }
        }
        const changeMedia = async (mount: boolean) => {
          if (!selected?.definition) return
          try {
            if (handlingClose || root !== runtimeRoot) throw new Error('后台连接已改变，请重新打开菜单')
            await call(root, mount ? 'vms.tools.mount' : 'vms.tools.eject', selected.vm_id, {
              revision: selected.definition.revision,
              instance_id: ['STOPPED', 'FAILED'].includes(selected.status.state) ? null : selected.status.instance_id
            })
            if (mount) await dialog.showMessageBox(window, { type: 'info', title: '安装增强工具与驱动', message: selected.status.state === 'RUNNING' ? '增强工具光盘已装载' : '增强工具光盘将在下次启动时装载', detail: `虚拟机：${selected.name}\n在 Windows 客户机中打开工具光驱，运行 Install.cmd 并确认管理员权限。完成后按安装程序提示重启客户机。`, buttons: ['知道了'] })
          } catch (error) {
            await dialog.showMessageBox(window, { type: 'error', title: '增强工具光盘', message: '工具盘操作失败', detail: error instanceof Error ? error.message : String(error), buttons: ['知道了'] })
          }
        }
        Menu.buildFromTemplate([
        { label: '文件', submenu: [{ label: '关闭窗口', click: () => window.close() }] },
        { label: '虚拟机', submenu: [
          { label: selected?.name ?? (vmId ? '虚拟机不可用' : '请先选择虚拟机'), enabled: false },
          { type: 'separator' },
          { id: 'install-windows-tools', label: '安装增强工具与驱动', enabled: !!selected?.persistent && selected.definition?.draft.os_preset !== 'linux' && selected.status.guest_arch === 'x86_64' && ['RUNNING', 'STOPPED', 'FAILED'].includes(selected.status.state), click: () => { void changeMedia(true) } },
          { id: 'eject-windows-tools', label: '弹出增强工具光盘', enabled: !!selected?.definition?.tools_iso_attached && ['RUNNING', 'STOPPED', 'FAILED'].includes(selected.status.state), click: () => { void changeMedia(false) } }
        ] },
        { label: '编辑', submenu: [{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' }, { type: 'separator' }, { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' }, { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' }] },
        { label: '视图', submenu: [{ role: 'resetZoom', label: '实际大小' }, { role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' }, { role: 'togglefullscreen', label: '切换全屏' }] },
        { label: '帮助', submenu: [{ label: `Palette ${app.getVersion()}`, enabled: false }] }
        ]).popup({ window })
      }
      else throw new Error('窗口操作无效')
      return { accepted: true }
    }
    if (operation.startsWith('conversations.')) {
      const repository = conversations()
      if (operation === 'conversations.list') return { conversations: await repository.list() }
      if (!vmId) throw new Error('请选择虚拟机')
      const listed = await call(runtimeRoot, 'vms.list') as { vms: { vm_id: string }[] }
      if (!listed.vms.some(vm => vm.vm_id === vmId)) throw new Error('虚拟机不存在')
      if (operation === 'conversations.create') return repository.create(vmId, payload.requestId, payload.draft)
      if (operation === 'conversations.update') {
        if ((payload.patch as { archived?: boolean } | undefined)?.archived === true) await agentServices.get(resolve(configRoot))?.stop(vmId, payload.id as string)
        return repository.update(vmId, payload.id, payload.revision, payload.patch)
      }
      if (operation === 'conversations.delete') {
        const service = agent()
        await service.snapshot(vmId, payload.id as string)
        await service.stop(vmId, payload.id as string)
        await repository.remove(vmId, payload.id, payload.revision)
        await service.remove(vmId, payload.id as string)
        return { deleted: true }
      }
      if (operation === 'conversations.export') {
        const item = (await repository.list()).find(item => item.id === payload.id && item.vm_id === vmId)
        if (!item) throw new Error('对话不存在')
        const destination = await dialog.showSaveDialog(window, { defaultPath: `${item.title.replace(/[\\/:*?"<>|]/g, '_')}.md`, filters: [{ name: 'Markdown', extensions: ['md'] }] })
        if (destination.canceled || !destination.filePath) return { saved: false }
        const history = await agent().exportMarkdown(vmId, item.id)
        await writeFile(destination.filePath, `${history}${item.draft ? `\n## 草稿\n\n${item.draft}\n` : ''}`, { mode: 0o600 })
        return { saved: true }
      }
      throw new Error('不支持的会话操作')
    }
    const repository = conversations()
    if (vmId && ['control.acquire', 'vms.stop', 'vms.restart', 'vms.reset', 'vms.delete'].includes(operation)) await agentServices.get(resolve(configRoot))?.stopVm(vmId)
    let expectedGeneration: string | undefined
    if (method === 'vms.stop' && payload.expected_instance_id !== undefined) {
      const owner = await call(runtimeRoot, 'hello')
      if (!Array.isArray(owner.features) || !owner.features.includes('vm-stop-identity')) throw new Error('后台需要升级才能校验关机目标，请重新连接后台后重试')
      expectedGeneration = String(owner.generation)
    }
    const result = await call(runtimeRoot, method, vmId, payload, expectedGeneration)
    if (method === 'control.acquire' && vmId) {
      leases.set(vmId, String(result.lease_id))
      if (!window.isFocused() || handlingClose) {
        try {
          const released = await call(runtimeRoot, 'control.release', vmId, { lease_id: result.lease_id })
          if (released.release_confirmed === true) leases.delete(vmId)
        } finally { throw new Error('窗口已失去焦点，请重新接管') }
      }
    }
    if (method === 'vms.delete' && vmId) { await agent().removeVm(vmId); await repository.removeVm(vmId) }
    if (method === 'control.release' && vmId) {
      if (result.release_confirmed !== true) throw new Error('输入释放尚未确认，已暂停写入；请检查虚拟机连接')
      leases.delete(vmId)
    }
    return result
  })
  ipcMain.handle('guest-cancel', (event, vmId: string) => { trusted(event); guestActions.get(vmId)?.abort() })
  ipcMain.handle('guest-action', async (event, vmId: string, instanceId: string, action: GuestAction, options: { path?: string; readOnly?: boolean; text?: string } = {}) => {
    trusted(event)
    if (handlingClose || !/^[A-Za-z0-9_.-]{1,128}$/.test(vmId) || typeof instanceId !== 'string' || instanceId.length > 128 || !['copy', 'paste', 'text', 'sync', 'upload', 'download', 'share', 'unshare'].includes(action)) throw new Error('增强操作无效')
    if (typeof options !== 'object' || options === null || Array.isArray(options) || Buffer.byteLength(JSON.stringify(options)) > 64 * 1024) throw new Error('增强参数无效')
    if (guestActions.has(vmId)) throw new Error('该虚拟机已有增强操作正在进行')
    const root = runtimeRoot
    const owner = await call(root, 'hello')
    if (!Array.isArray(owner.features) || !owner.features.includes('guest-enhancements-v1')) throw new Error('后台需要重启才能启用增强工具')
    const generation = String(owner.generation)
    const request = (method: string, payload: Record<string, unknown>): Promise<Record<string, unknown>> => call(root, method, vmId, payload, generation)
    let path: string | undefined
    if (action === 'upload' || action === 'download') checkedGuestPath(options.path)
    if (action === 'text') checkedClipboard(typeof options.text === 'string' ? options.text : '')
    await releaseAll(); window.webContents.send('control-lost')
    await agentServices.get(resolve(configRoot))?.stopVm(vmId)
    if (action === 'upload' || action === 'share') {
      const result = await dialog.showOpenDialog(window, { title: action === 'share' ? '选择共享目录' : '选择要发送的文件（最多 16 MiB）', properties: action === 'share' ? ['openDirectory'] : ['openFile'] })
      if (result.canceled || !result.filePaths[0]) return { completed: false }
      path = result.filePaths[0]
    } else if (action === 'download') {
      const result = await dialog.showSaveDialog(window, { title: '保存客户机文件' })
      if (result.canceled || !result.filePath) return { completed: false }
      path = result.filePath
    }
    if (!window.isFocused() || handlingClose) throw new Error('请回到 Palette 窗口后重试')
    if (guestActions.has(vmId)) throw new Error('该虚拟机已有增强操作正在进行')
    const controller = new AbortController()
    guestActions.set(vmId, controller)
    const progress = (bytes: number): void => { if (!window.isDestroyed()) window.webContents.send('guest-progress', { vmId, bytes, phase: 'running' }) }
    try {
      await withGuestLease(request, instanceId, controller.signal, async controlled => {
        if (action === 'paste' || action === 'text') await controlled('control.clipboard.set', { text: checkedClipboard(action === 'paste' ? clipboard.readText() : options.text ?? '') })
        else if (action === 'copy') {
          const result = await controlled('control.clipboard.get', {})
          if (typeof result.text !== 'string') throw new Error('客户机剪贴板无效')
          clipboard.writeText(checkedClipboard(result.text))
        } else if (action === 'sync') await controlled('guest.sync_time', {})
        else if (action === 'upload') await uploadGuestFile(controlled, path!, options.path!, controller.signal, progress)
        else if (action === 'download') await downloadGuestFile(controlled, path!, options.path!, controller.signal, progress)
        else await controlled('guest.share.set', { folders: action === 'unshare' ? [] : [{ tag: 'shared', path, read_only: options.readOnly !== false }] })
      })
      return { completed: true }
    } finally {
      guestActions.delete(vmId)
      if (!window.isDestroyed()) window.webContents.send('guest-progress', { vmId, bytes: 0, phase: 'done' })
    }
  })
  ipcMain.handle('choose-file', async (event, source: string) => {
    trusted(event)
    if (source !== 'image' && source !== 'iso') throw new Error('文件类型无效')
    const result = await dialog.showOpenDialog(window, { properties: ['openFile'], filters: [{ name: source === 'iso' ? 'ISO' : 'Disk image', extensions: source === 'iso' ? ['iso'] : ['qcow2', 'raw', 'img'] }] })
    return result.canceled ? null : result.filePaths[0]
  })
  ipcMain.handle('choose-root', async event => {
    trusted(event)
    if (guestActions.size) throw new Error('请先结束文件传输或增强操作后再切换工作目录')
    if (handlingClose) throw new Error('正在退出，请稍候')
    const result = await dialog.showOpenDialog(window, { properties: ['openDirectory'] })
    if (result.canceled) return null
    if (handlingClose) throw new Error('正在退出，请稍候')
    await releaseAll()
    await stopAgents()
    const previous = configRoot
    const previousRuntime = runtimeRoot
    try {
      configRoot = result.filePaths[0]
      const owner = await connectOwner()
      await mkdir(app.getPath('userData'), { recursive: true })
      await writeFile(join(app.getPath('userData'), 'settings.json'), JSON.stringify({ configRoot }))
      return owner
    } catch (error) { configRoot = previous; runtimeRoot = previousRuntime; throw error }
  })
  ipcMain.handle('finish-close', async (event, choice: string) => {
    trusted(event)
    if (handlingClose) throw new Error('正在退出，请稍候')
    if (choice === 'cancel') return
    if (!['background', 'shutdown'].includes(choice)) throw new Error('退出选项无效')
    if (choice === 'shutdown' && connectionsInFlight) throw new Error('正在连接后台，请等待连接完成后重试退出')
    const closingRuntime = runtimeRoot
    handlingClose = true
    try {
      await stopAgents()
      await releaseAll()
      if (choice === 'shutdown') await shutdown.stop(closingRuntime)
      allowedClose = true
      window.close()
    } catch (error) {
      if (error instanceof VmShutdownTimeout) return { shutdownTimeout: error.target }
      throw error
    } finally {
      handlingClose = false
    }
  })
  if (process.env.ELECTRON_RENDERER_URL) await window.loadURL(process.env.ELECTRON_RENDERER_URL)
  else await window.loadURL('app://palette/index.html')
})
app.on('window-all-closed', () => app.quit())
