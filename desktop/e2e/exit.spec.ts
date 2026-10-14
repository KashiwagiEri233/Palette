import { test, expect, _electron as electron, type ElectronApplication, type Page } from '@playwright/test'
import { mkdtemp, rm, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { execFileSync, spawn } from 'node:child_process'

const repository = resolve('..')
const uv = process.env.PALETTE_UV ?? 'uv'

function backendInfo(root: string): { pid: number; owner_kind: string } | null {
  try { return JSON.parse(execFileSync(uv, ['run', 'palette', '--root', root, 'daemon', 'status'], { cwd: repository, stdio: ['ignore', 'pipe', 'pipe'] }).toString()) }
  catch { return null }
}

async function fixture(): Promise<string> {
  const root = await mkdtemp(join(tmpdir(), 'palette-exit-'))
  execFileSync(uv, ['run', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  return root
}

async function launch(root: string): Promise<ElectronApplication> {
  const application = await electron.launch({ args: [resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  try {
    await expect((await application.firstWindow()).getByText(/^(后台已连接|由 MCP 会话托管)$/)).toBeVisible({ timeout: 30000 })
    return application
  } catch (error) {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => {})
    await application.close().catch(() => {})
    throw error
  }
}

async function closePrompt(application: ElectronApplication): Promise<Page> {
  const page = await application.firstWindow()
  await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close())
  await expect(page.getByRole('heading', { name: '退出 Palette？', exact: true })).toBeVisible()
  return page
}

async function screenshot(page: Page, path: string): Promise<void> {
  await page.evaluate(async () => {
    await document.fonts.ready
    await Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {})))
  })
  await page.screenshot({ path })
}

async function exit(application: ElectronApplication, choice: string): Promise<void> {
  const closed = application.waitForEvent('close')
  await (await application.firstWindow()).getByRole('button', { name: choice, exact: true }).click()
  await closed
}

async function cleanup(root: string, applications: ElectronApplication[]): Promise<void> {
  for (const application of applications) {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => {})
    await application.close().catch(() => {})
  }
  try { execFileSync(uv, ['run', 'palette', '--root', root, 'daemon', 'stop'], { cwd: repository, stdio: 'ignore' }) } catch { /* Already stopped. */ }
  if (!resolve(root).startsWith(join(tmpdir(), 'palette-exit-'))) throw new Error('Unexpected fixture cleanup root')
  await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
}

test('empty window always confirms, cancels repeatedly and completely stops its daemon', async () => {
  const root = await fixture()
  const applications: ElectronApplication[] = []
  try {
    const application = await launch(root)
    applications.push(application)
    const pid = backendInfo(root)!.pid
    const page = await closePrompt(application)
    await screenshot(page, 'test-results/exit-empty.png')
    await page.getByRole('button', { name: '取消', exact: true }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    await closePrompt(application)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).toBeHidden()
    await closePrompt(application)
    await application.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].close(); BrowserWindow.getAllWindows()[0].close() })
    await expect(page.getByRole('dialog')).toHaveCount(1)
    expect(backendInfo(root)!.pid).toBe(pid)
    await exit(application, '完全退出')
    expect(backendInfo(root)).toBeNull()
    expect(() => process.kill(pid, 0)).toThrow()
  } finally { await cleanup(root, applications) }
})

test('running VM survives window-only exit; shutdown failure preserves window and retries safely', async () => {
  test.setTimeout(120000)
  const root = await fixture()
  const applications: ElectronApplication[] = []
  try {
    let application = await launch(root)
    applications.push(application)
    let page = await application.firstWindow()
    const vmId = await page.evaluate(async path => {
      const vm = await window.palette.request('vms.create', undefined, { name: '退出测试', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
      return vm.vm_id
    }, join(root, 'base.qcow2'))
    await closePrompt(application)
    await screenshot(page, 'test-results/exit-stopped.png')
    await page.getByRole('button', { name: '取消', exact: true }).click()
    await page.evaluate(async vmId => { await window.palette.request('vms.start', vmId) }, vmId)
    const instance = await page.evaluate(async vmId => {
      const { vms } = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { instance_id: string } }[] }
      return vms.find(vm => vm.vm_id === vmId)!.status.instance_id
    }, vmId)
    const pid = backendInfo(root)!.pid
    await closePrompt(application)
    await exit(application, '仅关闭窗口')
    expect(backendInfo(root)!.pid).toBe(pid)
    application = await launch(root)
    applications.push(application)
    page = await application.firstWindow()
    const current = await page.evaluate(async vmId => {
      const { vms } = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { state: string; instance_id: string } }[] }
      return vms.find(vm => vm.vm_id === vmId)!.status
    }, vmId)
    expect(current.state).toBe('RUNNING')
    expect(current.instance_id).toBe(instance)
    const definitionPath = join(root, '.runtime', 'definitions', `${vmId}.json`)
    const definition = JSON.parse(await readFile(definitionPath, 'utf8'))
    const disk = join(root, '.runtime', 'vms', vmId, definition.storage_owner, 'disk', 'overlay.qcow2')
    const diskBefore = await readFile(disk)
    await writeFile(`${disk}.behavior.json`, JSON.stringify({ ignore_powerdown: true }))
    await closePrompt(application)
    await screenshot(page, 'test-results/exit-running.png')
    await page.getByRole('button', { name: '完全退出', exact: true }).click()
    await expect(page.getByRole('button', { name: '正在退出…', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '仅关闭窗口', exact: true })).toBeDisabled()
    await expect(page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true })).toBeDisabled()
    await page.keyboard.press('Escape')
    await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close())
    const duplicate = await page.evaluate(async () => { try { await window.palette.finishClose('shutdown'); return '' } catch (error) { return String(error) } })
    expect(duplicate).toContain('正在退出')
    await expect(page.getByRole('dialog').getByRole('alert')).toBeVisible({ timeout: 15000 })
    await expect(page.getByRole('button', { name: '完全退出', exact: true })).toBeEnabled()
    expect(backendInfo(root)!.pid).toBe(pid)
    await screenshot(page, 'test-results/exit-failure.png')
    await writeFile(`${disk}.behavior.json`, '{}')
    await exit(application, '完全退出')
    expect(backendInfo(root)).toBeNull()
    expect(await readFile(disk)).toEqual(diskBefore)
    expect(JSON.parse(await readFile(definitionPath, 'utf8')).vm_id).toBe(vmId)
  } finally { await cleanup(root, applications) }
})

test('complete exit stops the embedded MCP process while its stdin remains open', async () => {
  test.setTimeout(90000)
  const root = await fixture()
  const applications: ElectronApplication[] = []
  const embedded = spawn(uv, ['run', 'palette', '--root', root, 'serve', '--standalone'], { cwd: repository, windowsHide: true, stdio: 'pipe' })
  embedded.stdout.resume()
  embedded.stderr.resume()
  try {
    await expect.poll(() => backendInfo(root)?.owner_kind, { timeout: 30000 }).toBe('embedded')
    const application = await launch(root)
    applications.push(application)
    const pid = backendInfo(root)!.pid
    const page = await closePrompt(application)
    await expect(page.getByRole('dialog')).toContainText('中断相关 MCP 连接')
    await screenshot(page, 'test-results/exit-embedded.png')
    expect(embedded.stdin.destroyed).toBe(false)
    await exit(application, '完全退出')
    expect(backendInfo(root)).toBeNull()
    expect(() => process.kill(pid, 0)).toThrow()
    await expect.poll(() => embedded.exitCode).toBe(0)
  } finally {
    if (embedded.exitCode === null) { embedded.stdin.end(); await new Promise<void>(resolve => embedded.once('exit', () => resolve())) }
    await cleanup(root, applications)
  }
})

test('disconnected backend still prompts and permits closing the window after exit fails', async () => {
  const root = await fixture()
  const applications: ElectronApplication[] = []
  try {
    const application = await launch(root)
    applications.push(application)
    execFileSync(uv, ['run', 'palette', '--root', root, 'daemon', 'stop'], { cwd: repository })
    await expect.poll(() => backendInfo(root)).toBeNull()
    const page = await closePrompt(application)
    await page.getByRole('button', { name: '完全退出', exact: true }).click()
    await expect(page.getByRole('dialog').getByRole('alert')).toBeVisible()
    await exit(application, '仅关闭窗口')
    expect(backendInfo(root)).toBeNull()
  } finally { await cleanup(root, applications) }
})

test('older embedded backend requires upgrade and retains its service on window-only exit', async () => {
  test.setTimeout(90000)
  const root = await fixture()
  const applications: ElectronApplication[] = []
  // Negotiate an older owner's capabilities without changing repository source.
  const program = `from palette.runtime import RuntimeOwner
from palette.cli import main
original = RuntimeOwner.route
async def old_route(self, command):
    result = await original(self, command)
    if command.method == "hello":
        result["features"] = [feature for feature in result["features"] if feature != "runtime-stop"]
    return result
RuntimeOwner.route = old_route
raise SystemExit(main())`
  const embedded = spawn(uv, ['run', 'python', '-c', program, '--root', root, 'serve', '--standalone'], { cwd: repository, windowsHide: true, stdio: 'pipe' })
  embedded.stdout.resume()
  embedded.stderr.resume()
  try {
    await expect.poll(() => backendInfo(root)?.owner_kind, { timeout: 30000 }).toBe('embedded')
    const application = await launch(root)
    applications.push(application)
    const pid = backendInfo(root)!.pid
    const page = await closePrompt(application)
    await page.getByRole('button', { name: '完全退出', exact: true }).click()
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('需要升级')
    await screenshot(page, 'test-results/exit-upgrade.png')
    await exit(application, '仅关闭窗口')
    expect(backendInfo(root)!.pid).toBe(pid)
  } finally {
    if (embedded.exitCode === null) { embedded.stdin.end(); await new Promise<void>(resolve => embedded.once('exit', () => resolve())) }
    await cleanup(root, applications)
  }
})
