import { app, BrowserWindow, dialog, ipcMain, protocol, net } from 'electron'
import { spawn } from 'node:child_process'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, join, resolve, relative, isAbsolute } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { call } from './ipc'
import type { DesktopMethod, OwnerInfo, VmRecord } from '../shared/types'

const here = dirname(fileURLToPath(import.meta.url))
const backendRoot = resolve(here, '../../..')
let configRoot = process.env.PALETTE_ROOT ?? backendRoot
let runtimeRoot = join(configRoot, '.runtime')
let window: BrowserWindow
let allowedClose = false
let handlingClose = false
const leases = new Map<string, string>()
const methods = new Set<DesktopMethod>(['profiles', 'doctor', 'vms.list', 'vms.create', 'vms.update', 'vms.start', 'vms.stop', 'vms.restart', 'vms.reset', 'vms.delete', 'display.ticket', 'display.sync', 'control.acquire', 'control.input', 'control.heartbeat', 'control.release'])
methods.add('image.info')
methods.add('hardware.capabilities')
methods.add('vms.settings.get')
methods.add('vms.disk.resize')
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
    width: 1440, height: 900, minWidth: 960, minHeight: 640,
    title: 'Palette', backgroundColor: '#ffffff',
    titleBarStyle: process.platform === 'win32' ? 'hidden' : process.platform === 'darwin' ? 'hiddenInset' : 'default',
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
  window.on('blur', () => { void releaseAll(); window.webContents.send('control-lost') })
  window.webContents.on('render-process-gone', () => { void releaseAll() })
  window.on('close', event => {
    if (allowedClose) return
    event.preventDefault()
    if (handlingClose) return
    handlingClose = true
    void releaseAll().then(async () => {
      try {
        const response = await call(runtimeRoot, 'vms.list')
        const vms = response.vms as VmRecord[]
        if (vms.some(vm => !['STOPPED', 'FAILED'].includes(vm.status.state))) window.webContents.send('close-request')
        else { allowedClose = true; window.close() }
      } catch { window.webContents.send('close-request') }
    }).finally(() => { handlingClose = false })
  })
  ipcMain.handle('connect', event => { trusted(event); return connectOwner() })
  ipcMain.handle('window-backdrop', (event, opacity: unknown) => {
    trusted(event)
    if (typeof opacity !== 'number' || !Number.isFinite(opacity) || opacity < 0 || opacity > 1) throw new Error('窗口遮罩参数无效')
    if (process.platform !== 'win32') return
    const shade = Math.round(255 * (1 - opacity))
    const channel = shade.toString(16).padStart(2, '0')
    window.setTitleBarOverlay({ color: `#${channel.repeat(3)}`, symbolColor: shade < 128 ? '#e5e5e5' : '#252525' })
  })
  ipcMain.handle('request', async (event, method: DesktopMethod, vmId?: string, payload: Record<string, unknown> = {}) => {
    trusted(event)
    if (!methods.has(method) || (vmId !== undefined && !/^[A-Za-z0-9_.-]{1,128}$/.test(vmId)) || typeof payload !== 'object' || payload === null || Array.isArray(payload)) throw new Error('请求无效')
    const result = await call(runtimeRoot, method, vmId, payload)
    if (method === 'control.acquire' && vmId) leases.set(vmId, String(result.lease_id))
    if (method === 'control.release' && vmId) {
      if (result.release_confirmed !== true) throw new Error('输入释放尚未确认，已暂停写入；请检查虚拟机连接')
      leases.delete(vmId)
    }
    return result
  })
  ipcMain.handle('choose-file', async (event, source: string) => {
    trusted(event)
    if (source !== 'image' && source !== 'iso') throw new Error('文件类型无效')
    const result = await dialog.showOpenDialog(window, { properties: ['openFile'], filters: [{ name: source === 'iso' ? 'ISO' : 'Disk image', extensions: source === 'iso' ? ['iso'] : ['qcow2', 'raw', 'img'] }] })
    return result.canceled ? null : result.filePaths[0]
  })
  ipcMain.handle('choose-root', async event => {
    trusted(event)
    const result = await dialog.showOpenDialog(window, { properties: ['openDirectory'] })
    if (result.canceled) return null
    await releaseAll()
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
    if (choice === 'cancel') return
    if (!['background', 'shutdown'].includes(choice)) throw new Error('退出选项无效')
    await releaseAll()
    if (choice === 'shutdown') {
      const response = await call(runtimeRoot, 'vms.list')
      for (const vm of response.vms as VmRecord[]) if (!['STOPPED', 'FAILED'].includes(vm.status.state)) await call(runtimeRoot, 'vms.stop', vm.vm_id)
      const owner = await call(runtimeRoot, 'hello')
      if (owner.owner_kind === 'daemon') await call(runtimeRoot, 'daemon.stop')
    }
    allowedClose = true; window.close()
  })
  if (process.env.ELECTRON_RENDERER_URL) await window.loadURL(process.env.ELECTRON_RENDERER_URL)
  else await window.loadURL('app://palette/index.html')
})
app.on('window-all-closed', () => app.quit())
