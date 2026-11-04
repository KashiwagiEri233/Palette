import { test, expect, _electron as electron, type ElectronApplication } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdir, mkdtemp, rm } from 'node:fs/promises'
import { join, relative, resolve } from 'node:path'
import type { DisplaySettings } from '../src/shared/types'

test('display settings show effective instances, retain failed changes and gate old backends', async ({}, testInfo) => {
  test.setTimeout(120000)
  const repository = resolve('..'), parent = join(repository, '.runtime', 'desktop-display-settings-tests')
  await mkdir(parent, { recursive: true })
  const root = await mkdtemp(join(parent, 'settings-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  const environment = { ...process.env, UV_CACHE_DIR: join(parent, 'uv-cache'), PALETTE_ROOT: root, PALETTE_UV: uv }
  let application: ElectronApplication | undefined
  try {
    execFileSync(uv, ['run', '--offline', 'python', join(repository, 'desktop/scripts/setup_fixture.py'), root], { cwd: repository, env: environment })
    application = await electron.launch({ args: [`--user-data-dir=${join(root, 'electron')}`, resolve('out/main/index.js')], env: environment })
    const page = await application.firstWindow()
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await expect(page.getByText('后台已连接', { exact: true })).toBeVisible({ timeout: 30000 })
    // This read exercises the real main-process allowlist and authenticated backend.
    expect(await page.evaluate(() => window.palette.request('display.settings.get'))).toMatchObject({ configured_mode: 'inherit', instances: [] })
    const owner = await page.evaluate(() => window.palette.connect())
    expect(owner.features).toContain('display-settings-v1')
    await application.evaluate(({ ipcMain }) => {
      type Handler = (...args: unknown[]) => unknown
      const original = (ipcMain as unknown as { _invokeHandlers: Map<string, Handler> })._invokeHandlers.get('request')!
      const state = {
        settings: { configured_mode: 'inherit', instances: [{ vm_id: 'vm-display-test', instance_id: 'inst-current', provider: 'rfb', refresh_mode: 'adaptive', requested_mode: 'inherit', restart_required: false, reason: null }] } as DisplaySettings,
        calls: [] as { method: string; vmId?: string; payload?: Record<string, unknown> }[],
        failNextUpdate: false
      }
      Object.assign(globalThis, { displaySettingsTest: state })
      ipcMain.removeHandler('request')
      ipcMain.handle('request', async (event, method: string, vmId?: string, payload?: Record<string, unknown>) => {
        state.calls.push({ method, vmId, payload })
        if (method === 'display.settings.get') return state.settings
        if (method === 'display.settings.update') {
          if (state.failNextUpdate) { state.failNextUpdate = false; throw new Error('Display settings are read-only') }
          state.settings = {
            configured_mode: payload!.mode as DisplaySettings['configured_mode'],
            instances: [{ ...state.settings.instances[0], restart_required: true, reason: 'D-Bus display is unavailable; compatibility display remains active' }]
          }
          return state.settings
        }
        return await original(event, method, vmId, payload)
      })
    })
    await page.getByRole('button', { name: '设置', exact: true }).click()
    const toggle = page.getByRole('switch', { name: '低延迟显示', exact: true })
    await expect(toggle).toBeEnabled()
    await expect(toggle).not.toBeChecked()
    await expect(page.getByText('当前沿用已有显示配置。开启后使用低延迟模式，关闭后使用兼容模式。', { exact: true })).toBeVisible()
    await expect(page.getByText('显示模式在关闭虚拟机后重新启动时生效。客户机内重启不会应用此设置。', { exact: true })).toBeVisible()
    await toggle.click()
    await expect(toggle).toBeChecked()
    await expect(page.getByText('显示设置已保存，将在下次启动虚拟机时应用。', { exact: true })).toBeVisible()
    const instance = page.locator('.display-settings-instances tbody tr').filter({ hasText: 'vm-display-test' })
    await expect(instance).toContainText('待关闭后重新启动')
    await expect(instance).toContainText('VNC · 自适应刷新')
    await expect(instance).toContainText('D-Bus display is unavailable')
    await page.screenshot({ path: testInfo.outputPath('display-settings.png'), animations: 'disabled' })
    await application.evaluate(() => { (globalThis as unknown as { displaySettingsTest: { failNextUpdate: boolean } }).displaySettingsTest.failNextUpdate = true })
    await toggle.click()
    await expect(page.getByRole('alert')).toContainText('Display settings are read-only')
    await expect(toggle).toBeEnabled()
    await expect(toggle).toBeChecked()
    await toggle.click()
    await expect(toggle).not.toBeChecked()
    await expect(page.getByRole('alert')).toHaveCount(0)
    await application.evaluate(() => {
      const state = (globalThis as unknown as { displaySettingsTest: { settings: DisplaySettings } }).displaySettingsTest
      state.settings = { configured_mode: 'low_latency', instances: [{ ...state.settings.instances[0], requested_mode: 'low_latency', refresh_mode: 'steady', restart_required: false, reason: 'Display settings were saved, but power-loss durability could not be confirmed' }] }
    })
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    await page.getByRole('button', { name: '设置', exact: true }).click()
    await expect(instance).toContainText('VNC · 持续刷新')
    await expect(instance).toContainText('已按当前配置运行')
    await expect(instance).not.toContainText('已降级运行')
    await expect(instance).toContainText('power-loss durability could not be confirmed')
    const calls = await application.evaluate(() => (globalThis as unknown as { displaySettingsTest: { calls: { method: string; vmId?: string; payload?: Record<string, unknown> }[] } }).displaySettingsTest.calls)
    expect(calls.filter(call => call.method === 'display.settings.update')).toEqual([
      { method: 'display.settings.update', vmId: undefined, payload: { mode: 'low_latency' } },
      { method: 'display.settings.update', vmId: undefined, payload: { mode: 'compatibility' } },
      { method: 'display.settings.update', vmId: undefined, payload: { mode: 'compatibility' } }
    ])
    expect(calls.some(call => /^vms\.(start|stop|restart|reboot|reset)$/.test(call.method))).toBe(false)
    const reads = calls.filter(call => call.method === 'display.settings.get').length
    await application.evaluate(({ ipcMain }, current) => {
      ipcMain.removeHandler('connect')
      ipcMain.handle('connect', async () => ({ ...current, features: current.features.filter(feature => feature !== 'display-settings-v1') }))
    }, owner)
    await page.getByRole('button', { name: '重新连接', exact: true }).click()
    await expect(toggle).toBeDisabled()
    await expect(page.getByText('当前后台不支持此设置，请更新后台并重新连接。', { exact: true })).toBeVisible()
    expect(await application.evaluate(() => (globalThis as unknown as { displaySettingsTest: { calls: { method: string }[] } }).displaySettingsTest.calls.filter(call => call.method === 'display.settings.get').length)).toBe(reads)
    expect(errors).toEqual([])
  } finally {
    await application?.evaluate(({ app }) => app.exit(0)).catch(() => {})
    await application?.close().catch(() => {})
    const owned = relative(parent, root)
    if (!owned || owned.startsWith('..') || owned.includes('/') || owned.includes('\\')) throw new Error('Unexpected test root')
    try { execFileSync(uv, ['run', '--offline', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository, env: environment }) } catch { /* The test backend may already be stopped. */ }
    await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
  }
})
