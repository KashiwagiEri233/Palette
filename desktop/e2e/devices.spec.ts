import { test, expect, _electron as electron } from '@playwright/test'
import { mkdtemp, rm, writeFile, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { execFileSync } from 'node:child_process'

test('settings sidebar adds multiple drives, preserves IDs and removes devices without deleting media', async () => {
  test.setTimeout(120000)
  const repository = resolve('..')
  const root = await mkdtemp(join(tmpdir(), 'palette-devices-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  execFileSync(uv, ['run', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  const iso = join(root, 'extra.iso')
  await writeFile(iso, 'extra media fixture')
  const application = await electron.launch({ args: [resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  try {
    const page = await application.firstWindow()
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await expect(page.getByText('后台已连接')).toBeVisible({ timeout: 30000 })
    const vm = await page.evaluate(async path => window.palette.request('vms.create', undefined, { name: '多设备测试', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as Promise<{ vm_id: string }>, join(root, 'base.qcow2'))
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    await page.getByRole('button', { name: '多设备测试', exact: true }).click()
    async function openSettings() {
      await page.getByRole('button', { name: '虚拟机设置', exact: true }).click()
      await page.getByRole('dialog').getByRole('button', { name: '设备与驱动器', exact: true }).click()
    }
    await openSettings()
    const dialog = page.getByRole('dialog')
    await application.evaluate(({ dialog }) => { dialog.showOpenDialog = async () => ({ canceled: true, filePaths: [] }) })
    await dialog.getByRole('button', { name: '添加磁盘', exact: true }).click()
    await expect(dialog.locator('.drive-card')).toHaveCount(0)
    await application.evaluate(({ dialog }, path) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path] }) }, join(root, 'base.qcow2'))
    await dialog.getByRole('button', { name: '添加磁盘', exact: true }).click()
    await dialog.getByRole('button', { name: '添加磁盘', exact: true }).click()
    await dialog.getByRole('button', { name: '添加光驱', exact: true }).click()
    await dialog.getByRole('button', { name: '添加光驱', exact: true }).click()
    await application.evaluate(({ dialog }, path) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path] }) }, iso)
    await dialog.getByRole('region', { name: '附加光驱 3', exact: true }).getByRole('button', { name: '选择 ISO', exact: true }).click()
    await expect(dialog.locator('.drive-card')).toHaveCount(4)
    await dialog.locator('.vm-settings-body').evaluate(element => { element.scrollTop = 0 })
    await page.screenshot({ path: 'test-results/devices-and-drives.png', animations: 'disabled' })
    await dialog.getByRole('button', { name: '保存', exact: true }).click()
    await expect(dialog).toHaveCount(0)
    const getSettings = () => page.evaluate(async id => window.palette.request('vms.settings.get', id) as Promise<{ settings: { drives: { id: string; media: string; path: string | null }[] } }>, vm.vm_id)
    const original = (await getSettings()).settings.drives
    expect(original.map(item => item.media)).toEqual(['disk', 'disk', 'cdrom', 'cdrom'])
    await openSettings()
    await page.waitForTimeout(2200) // Cross the application's background refresh interval.
    await expect(dialog.locator('.drive-card')).toHaveCount(4)
    await dialog.getByRole('button', { name: '移除驱动器 3', exact: true }).click()
    await dialog.getByRole('button', { name: '保存', exact: true }).click()
    await expect(dialog).toHaveCount(0)
    expect((await getSettings()).settings.drives.map(item => item.id)).toEqual([original[0].id, original[1].id, original[3].id])
    expect(await readFile(iso, 'utf8')).toBe('extra media fixture')
    await page.getByRole('button', { name: '启动', exact: true }).click()
    await expect(page.getByText('仅查看', { exact: true })).toBeVisible({ timeout: 20000 })
    await openSettings()
    await expect(dialog.getByRole('button', { name: '添加磁盘', exact: true })).toBeDisabled()
    await expect(dialog.getByRole('button', { name: '添加光驱', exact: true })).toBeDisabled()
    expect(errors).toEqual([])
  } finally {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => undefined)
    await application.close().catch(() => undefined)
    execFileSync(uv, ['run', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository })
    await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
  }
})
