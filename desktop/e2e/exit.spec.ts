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

test('power buttons confirm forced shutdown, cancel safely and preserve persistent disks', async () => {
  test.setTimeout(120000)
  const root = await fixture()
  const applications: ElectronApplication[] = []
  try {
    const application = await launch(root)
    applications.push(application)
    const page = await application.firstWindow()
    const vmId = await page.evaluate(async path => {
      const vm = await window.palette.request('vms.create', undefined, { name: '电源键测试', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
      return vm.vm_id
    }, join(root, 'base.qcow2'))
    const status = () => page.evaluate(async id => {
      const { vms } = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { state: string; instance_id: string; process_running: boolean } }[] }
      return vms.find(vm => vm.vm_id === id)!.status
    }, vmId)
    for (const entry of ['list', 'console', 'menu']) {
      await page.evaluate(async id => { await window.palette.request('vms.start', id) }, vmId)
      const before = await status()
      const definition = JSON.parse(await readFile(join(root, '.runtime', 'definitions', `${vmId}.json`), 'utf8'))
      const disk = join(root, '.runtime', 'vms', vmId, definition.storage_owner, 'disk', 'overlay.qcow2')
      const diskBefore = await readFile(disk)
      await writeFile(`${disk}.behavior.json`, JSON.stringify({ ignore_powerdown: true }))
      await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
      const row = page.getByRole('row').filter({ hasText: '电源键测试' })
      await expect(row.getByText('运行中', { exact: true })).toBeVisible()
      if (entry !== 'list') await row.getByRole('button', { name: '打开', exact: true }).click()
      const pressPower = async () => {
        if (entry === 'list') await row.getByRole('button', { name: '关机', exact: true }).click()
        else if (entry === 'console') await page.locator('.console-panel').getByRole('button', { name: '关机', exact: true }).click()
        else {
          await page.locator('.desktop-main').getByRole('button', { name: '电源键测试 的操作', exact: true }).click()
          await page.getByRole('menuitem', { name: '关机', exact: true }).click()
        }
      }
      const confirmation = page.getByRole('dialog', { name: '强制停止虚拟机', exact: true })
      await pressPower()
      await expect(confirmation).toContainText('电源键测试')
      await expect(confirmation).toContainText('强制断电')
      await expect(confirmation).toContainText('尚未保存的数据可能丢失')
      await expect(confirmation).toContainText('可能损坏客户机文件系统')
      await confirmation.getByRole('button', { name: '取消', exact: true }).click()
      expect(await status()).toMatchObject({ state: 'RUNNING', instance_id: before.instance_id, process_running: true })
      await pressPower()
      await page.keyboard.press('Escape')
      await expect(confirmation).toBeHidden()
      expect((await status()).state).toBe('RUNNING')
      await pressPower()
      if (entry === 'console') await screenshot(page, 'test-results/power-force-confirmation.png')
      await confirmation.getByRole('button', { name: '确认', exact: true }).click()
      await expect(confirmation).toBeHidden({ timeout: 10000 })
      expect(await status()).toMatchObject({ state: 'STOPPED', process_running: false })
      expect(await readFile(disk)).toEqual(diskBefore)
      const commands = (await readFile(`${disk}.log.jsonl`, 'utf8')).trim().split('\n').map(line => JSON.parse(line).command)
      expect(commands).not.toContain('system_powerdown')
      expect(commands).toContain('quit')
    }
  } finally { await cleanup(root, applications) }
})

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
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('虚拟机未完成正常关机', { timeout: 15000 })
    await expect(page.getByRole('button', { name: '完全退出', exact: true })).toBeEnabled()
    await expect(page.getByRole('button', { name: '强制停止此虚拟机并退出…', exact: true })).toBeVisible()
    expect(backendInfo(root)!.pid).toBe(pid)
    await screenshot(page, 'test-results/exit-failure.png')
    await writeFile(`${disk}.behavior.json`, '{}')
    await exit(application, '完全退出')
    expect(backendInfo(root)).toBeNull()
    expect(await readFile(disk)).toEqual(diskBefore)
    expect(JSON.parse(await readFile(definitionPath, 'utf8')).vm_id).toBe(vmId)
  } finally { await cleanup(root, applications) }
})

test('quit during a manual shutdown stays responsive and refreshes the failed VM with the dialog open', async () => {
  const root = await fixture()
  const applications: ElectronApplication[] = []
  try {
    const profilePath = join(root, 'profiles', 'default.toml')
    await writeFile(profilePath, (await readFile(profilePath, 'utf8')).replace(/shutdown_seconds = [\d.]+/, 'shutdown_seconds = 5.0'))
    const application = await launch(root)
    applications.push(application)
    const page = await application.firstWindow()
    const vmId = await page.evaluate(async path => {
      const vm = await window.palette.request('vms.create', undefined, { name: '并发关机测试', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
      await window.palette.request('vms.start', vm.vm_id)
      return vm.vm_id
    }, join(root, 'base.qcow2'))
    const definition = JSON.parse(await readFile(join(root, '.runtime', 'definitions', `${vmId}.json`), 'utf8'))
    const disk = join(root, '.runtime', 'vms', vmId, definition.storage_owner, 'disk', 'overlay.qcow2')
    await writeFile(`${disk}.behavior.json`, JSON.stringify({ ignore_powerdown: true }))
    const pid = backendInfo(root)!.pid
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    const row = page.getByRole('row', { includeHidden: true }).filter({ hasText: '并发关机测试' })
    await expect(row.getByText('运行中', { exact: true })).toBeVisible()
    await row.getByRole('button', { name: '并发关机测试 的操作', exact: true }).click()
    await page.getByRole('menuitem', { name: '请求正常关机', exact: true }).click()
    await closePrompt(application)
    const dialog = page.getByRole('dialog')
    await dialog.getByRole('button', { name: '完全退出', exact: true }).click()
    await expect(dialog.getByRole('alert')).toContainText('虚拟机操作尚未完成', { timeout: 1000 })
    await expect(dialog.getByRole('button', { name: '仅关闭窗口', exact: true })).toBeEnabled()
    await expect(row.getByText('关机中', { exact: true })).toBeVisible()
    await expect(page.locator('.desktop-error')).toContainText('正常关机未完成', { timeout: 10000 })
    // Polling is suspended while the dialog is open; recovery must refresh explicitly.
    await expect(row.getByText('运行中', { exact: true })).toBeVisible()
    await expect(row.getByRole('button', { name: '关机', exact: true, includeHidden: true })).toBeEnabled()
    expect(backendInfo(root)!.pid).toBe(pid)
    await screenshot(page, 'test-results/exit-concurrent-shutdown.png')
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    await expect(dialog).toBeHidden()
    await page.getByRole('button', { name: '强制停止…', exact: true }).click()
    await expect(page.getByRole('dialog', { name: '强制停止虚拟机', exact: true })).toContainText('尚未保存的数据可能丢失')
    await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click()
    expect((await page.evaluate(async vmId => {
      const result = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { state: string } }[] }
      return result.vms.find(vm => vm.vm_id === vmId)!.status.state
    }, vmId))).toBe('RUNNING')
    await page.getByRole('button', { name: '强制停止…', exact: true }).click()
    await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click()
    await expect(page.getByRole('dialog')).toBeHidden()
    await expect(row.getByText('已关机', { exact: true })).toBeVisible()
    await closePrompt(application)
    await exit(application, '完全退出')
    expect(backendInfo(root)).toBeNull()
  } finally { await cleanup(root, applications) }
})

test('complete exit requires force confirmation for the timed-out VM and preserves both disks', async () => {
  test.setTimeout(120000)
  const root = await fixture()
  const applications: ElectronApplication[] = []
  try {
    const application = await launch(root)
    applications.push(application)
    const page = await application.firstWindow()
    const ids = await page.evaluate(async path => {
      const ids: string[] = []
      for (const name of ['超时客户机', '正常客户机']) {
        const vm = await window.palette.request('vms.create', undefined, { name, profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
        await window.palette.request('vms.start', vm.vm_id)
        ids.push(vm.vm_id)
      }
      return ids
    }, join(root, 'base.qcow2'))
    const disks = await Promise.all(ids.map(async id => {
      const definition = JSON.parse(await readFile(join(root, '.runtime', 'definitions', `${id}.json`), 'utf8'))
      const path = join(root, '.runtime', 'vms', id, definition.storage_owner, 'disk', 'overlay.qcow2')
      return { path, before: await readFile(path) }
    }))
    await writeFile(`${disks[0].path}.behavior.json`, JSON.stringify({ ignore_powerdown: true }))
    await closePrompt(application)
    await page.getByRole('button', { name: '完全退出', exact: true }).click()
    const forceExit = page.getByRole('button', { name: '强制停止此虚拟机并退出…', exact: true })
    await expect(forceExit).toBeVisible({ timeout: 15000 })
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('超时客户机')
    const healthy = await page.evaluate(async id => {
      const { vms } = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { state: string } }[] }
      return vms.find(vm => vm.vm_id === id)!.status.state
    }, ids[1])
    expect(healthy).toBe('STOPPED')
    await forceExit.click()
    const confirmation = page.getByRole('dialog', { name: '强制停止虚拟机', exact: true })
    await expect(confirmation).toContainText('超时客户机')
    await expect(confirmation).toContainText('尚未保存的数据可能丢失')
    await screenshot(page, 'test-results/exit-force-confirmation.png')
    await confirmation.getByRole('button', { name: '取消', exact: true }).click()
    const state = await page.evaluate(async id => {
      const { vms } = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { state: string } }[] }
      return vms.find(vm => vm.vm_id === id)!.status.state
    }, ids[0])
    expect(state).toBe('RUNNING')
    await closePrompt(application)
    await page.getByRole('button', { name: '完全退出', exact: true }).click()
    await expect(forceExit).toBeVisible({ timeout: 15000 })
    await forceExit.click()
    await expect(confirmation).toBeVisible()
    await exit(application, '确认')
    expect(backendInfo(root)).toBeNull()
    for (const disk of disks) expect(await readFile(disk.path)).toEqual(disk.before)
  } finally { await cleanup(root, applications) }
})

for (const action of ['关机', '重启'] as const) {
test(`force stop interrupts a pending ${action} without waiting for its timeout`, async () => {
  test.setTimeout(90000)
  const root = await fixture()
  const applications: ElectronApplication[] = []
  try {
    const profilePath = join(root, 'profiles', 'default.toml')
    await writeFile(profilePath, (await readFile(profilePath, 'utf8')).replace(/shutdown_seconds = [\d.]+/, 'shutdown_seconds = 60.0'))
    const application = await launch(root)
    applications.push(application)
    const page = await application.firstWindow()
    const vmId = await page.evaluate(async path => {
      const vm = await window.palette.request('vms.create', undefined, { name: '中断关机测试', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
      await window.palette.request('vms.start', vm.vm_id)
      return vm.vm_id
    }, join(root, 'base.qcow2'))
    const definition = JSON.parse(await readFile(join(root, '.runtime', 'definitions', `${vmId}.json`), 'utf8'))
    const disk = join(root, '.runtime', 'vms', vmId, definition.storage_owner, 'disk', 'overlay.qcow2')
    const before = await readFile(disk)
    await writeFile(`${disk}.behavior.json`, JSON.stringify({ ignore_powerdown: true }))
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    const row = page.getByRole('row').filter({ hasText: '中断关机测试' })
    await row.getByRole('button', { name: '打开', exact: true }).click()
    const consolePanel = page.locator('.console-panel')
    await expect(consolePanel.locator('canvas')).toBeVisible()
    if (action === '关机') {
      await page.locator('.desktop-main').getByRole('button', { name: '中断关机测试 的操作', exact: true }).click()
      await page.getByRole('menuitem', { name: '请求正常关机', exact: true }).click()
    } else {
      await consolePanel.getByRole('button', { name: action, exact: true }).click()
    }
    const progress = page.locator('.desktop-shutdown-status')
    await expect(progress).toContainText('正在等待')
    await expect(page.getByText('关机中', { exact: true })).toBeVisible()
    await expect(consolePanel.locator('canvas')).toBeVisible()
    await expect(consolePanel.getByText('仅查看', { exact: true })).toBeVisible()
    await expect(consolePanel.locator('.console-placeholder')).toHaveCount(0)
    await expect(consolePanel.getByRole('button', { name: '接管', exact: true })).toBeDisabled()
    await screenshot(page, `test-results/shutdown-uncovered-${action === '关机' ? 'stop' : 'restart'}.png`)
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    await expect(row.getByText('关机中', { exact: true })).toBeVisible()
    await progress.getByRole('button', { name: '强制停止…', exact: true }).click()
    const confirmation = page.getByRole('dialog', { name: '强制停止虚拟机', exact: true })
    await expect(confirmation.getByRole('button', { name: '确认', exact: true })).toBeEnabled()
    await confirmation.getByRole('button', { name: '取消', exact: true }).click()
    await expect(progress).toBeVisible()
    await progress.getByRole('button', { name: '强制停止…', exact: true }).click()
    await screenshot(page, action === '关机' ? 'test-results/shutdown-force-interrupt.png' : 'test-results/shutdown-restart-force-interrupt.png')
    await confirmation.getByRole('button', { name: '确认', exact: true }).click()
    await expect(confirmation).toBeHidden({ timeout: 6000 })
    await expect(row.getByText('已关机', { exact: true })).toBeVisible({ timeout: 6000 })
    await expect(progress).toHaveCount(0)
    await expect(page.locator('.desktop-error')).toHaveCount(0)
    expect(await readFile(disk)).toEqual(before)
    await closePrompt(application)
    await exit(application, '完全退出')
    expect(backendInfo(root)).toBeNull()
  } finally { await cleanup(root, applications) }
})
}

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
