import { test, expect, _electron as electron } from '@playwright/test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { execFileSync } from 'node:child_process'

test('agent enhancements need no manual tools panel and baseline control remains usable', async () => {
  test.setTimeout(120000)
  const repository = resolve('..')
  const root = await mkdtemp(join(tmpdir(), 'palette-enhancements-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  execFileSync(uv, ['run', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  const application = await electron.launch({ args: [resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  try {
    const page = await application.firstWindow()
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await expect(page.getByText('后台已连接')).toBeVisible({ timeout: 30000 })
    const vmId = await page.evaluate(async path => {
      const vm = await window.palette.request('vms.create', undefined, { name: '增强工具测试', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
      await window.palette.request('vms.start', vm.vm_id)
      return vm.vm_id
    }, join(root, 'base.qcow2'))
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    await expect(page.getByRole('button', { name: '增强工具测试', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '增强工具测试', exact: true }).click()
    await page.getByRole('tab', { name: '控制台', exact: true }).click()
    const canvas = page.getByRole('tabpanel', { name: '控制台', exact: true }).locator('canvas')
    await expect(canvas).toBeVisible({ timeout: 20000 })
    await expect(canvas).toHaveJSProperty('width', 320)
    await expect(page.getByRole('checkbox', { name: '自动分辨率' })).toBeDisabled()
    await expect(page.getByRole('button', { name: '增强工具', exact: true })).toHaveCount(0)
    await page.screenshot({ path: 'test-results/guest-enhancements-unavailable.png' })
    await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].focus())
    await page.getByRole('button', { name: '接管', exact: true }).click()
    await expect(page.getByText('人工控制', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '结束控制', exact: true }).click()
    expect(errors).toEqual([])
    await page.evaluate(async vmId => {
      const result = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { instance_id: string } }[] }
      await window.palette.request('vms.stop', vmId, { expected_instance_id: result.vms.find(vm => vm.vm_id === vmId)!.status.instance_id })
    }, vmId)
  } finally {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => undefined)
    await application.close()
    execFileSync(uv, ['run', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository })
    await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
  }
})
