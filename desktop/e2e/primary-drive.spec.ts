import { test, expect, _electron as electron } from '@playwright/test'
import { access, mkdtemp, rm, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { execFileSync } from 'node:child_process'
import type { VmSettingsResult } from '../src/shared/types'

test('initial disk shares drive editing, ordering, staged deletion and Cancel', async () => {
  test.setTimeout(120000)
  const repository = resolve('..')
  const root = await mkdtemp(join(tmpdir(), 'palette-primary-drive-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  execFileSync(uv, ['run', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  const source = join(root, 'base.qcow2')
  const original = await readFile(source)
  const app = await electron.launch({ args: [resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  try {
    const page = await app.firstWindow()
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await expect(page.getByText('后台已连接')).toBeVisible({ timeout: 30000 })
    const vmId = await page.evaluate(async path => {
      const vm = await window.palette.request('vms.create', undefined, { name: '统一驱动器测试', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
      const data = await window.palette.request('vms.settings.get', vm.vm_id) as VmSettingsResult
      await window.palette.request('vms.update', vm.vm_id, { revision: data.revision, name: data.draft.name, vcpus: 1, memory_mib: 512, network: 'offline', iso_attached: false, settings: { ...data.settings, drives: [...data.settings.drives!, { id: 'drive-data', media: 'disk', size_gib: 1 }] } })
      await window.palette.request('vms.start', vm.vm_id)
      await window.palette.request('vms.stop', vm.vm_id)
      return vm.vm_id
    }, source)
    const data = () => page.evaluate(async id => window.palette.request('vms.settings.get', id) as Promise<VmSettingsResult>, vmId)
    const initial = await data()
    const initialPath = initial.managed_disks!['drive-primary'].path!
    const dataPath = initial.managed_disks!['drive-data'].path!
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    await page.getByRole('button', { name: '统一驱动器测试', exact: true }).click()
    const settings = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: '虚拟机设置', exact: true }) })
    const confirmation = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: '删除驱动器？', exact: true }) })
    const open = async () => { await page.getByRole('button', { name: '虚拟机设置', exact: true }).click(); await expect(settings.getByRole('navigation')).toBeVisible() }
    await open()
    await settings.getByRole('button', { name: 'VirtIO 磁盘 1', exact: true }).click()
    await expect(settings.getByRole('button', { name: '扩容磁盘', exact: true })).toBeEnabled()
    await settings.getByRole('combobox', { name: '接口', exact: true }).click()
    await page.getByRole('option', { name: 'IDE', exact: true }).click()
    await settings.getByRole('combobox', { name: '访问方式', exact: true }).click()
    await page.getByRole('option', { name: '只读', exact: true }).click()
    await settings.getByRole('button', { name: 'IDE 磁盘 1 的操作', exact: true }).click()
    await page.getByRole('menuitem', { name: '下移', exact: true }).click()
    await page.screenshot({ path: 'test-results/primary-drive-edit.png', animations: 'disabled' })
    await settings.getByRole('button', { name: '保存', exact: true }).click()
    await expect(settings).toHaveCount(0)
    const saved = await data()
    expect(saved.settings.drives?.map(drive => drive.id)).toEqual(['drive-data', 'drive-primary'])
    expect(saved.settings.drives?.[1]).toMatchObject({ interface: 'ide', read_only: true })
    const remove = async () => {
      await settings.getByRole('button', { name: 'IDE 磁盘 1', exact: true }).click()
      await settings.getByRole('button', { name: '删除驱动器…', exact: true }).click()
      await expect(confirmation).toContainText('永久删除此磁盘文件')
      await confirmation.getByRole('button', { name: '删除', exact: true }).click()
      await expect(settings.locator('[data-drive-id="drive-primary"]')).toHaveCount(0)
    }
    await open()
    await settings.getByRole('button', { name: 'VirtIO 磁盘 1', exact: true }).click()
    await settings.getByRole('button', { name: '扩容磁盘', exact: true }).click()
    await page.getByLabel('目标容量 / GiB').fill('2')
    await page.getByRole('button', { name: '确认扩容', exact: true }).click()
    await expect(settings.getByRole('status')).toContainText('磁盘已扩容')
    const expanded = await data()
    expect(expanded.managed_disks!['drive-data'].size_gib).toBe(2)
    expect(expanded.managed_disks!['drive-primary'].size_gib).toBe(24)
    await remove()
    await access(initialPath)
    await settings.getByRole('button', { name: '取消', exact: true }).click()
    await page.getByRole('button', { name: '丢弃修改', exact: true }).click()
    await access(initialPath)
    expect((await data()).settings.drives).toHaveLength(2)
    await open(); await remove()
    await settings.getByRole('button', { name: '保存', exact: true }).click()
    await expect(settings).toHaveCount(0)
    await expect(access(initialPath)).rejects.toThrow()
    await access(dataPath)
    expect(await readFile(source)).toEqual(original)
    await open()
    await expect(settings.locator('[data-drive-id]')).toHaveCount(1)
    await expect(settings.getByRole('button', { name: 'VirtIO 磁盘 1', exact: true })).toBeVisible()
    await page.screenshot({ path: 'test-results/primary-drive-deleted.png', animations: 'disabled' })
    await settings.getByRole('button', { name: '取消', exact: true }).click()
    await page.getByRole('button', { name: '启动', exact: true }).click()
    await expect(page.getByText('仅查看', { exact: true })).toBeVisible({ timeout: 20000 })
    await access(dataPath)
    await expect(access(initialPath)).rejects.toThrow()
  } finally {
    await app.evaluate(({ app }) => app.exit(0)).catch(() => undefined)
    await app.close().catch(() => undefined)
    execFileSync(uv, ['run', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository })
    if (!resolve(root).startsWith(join(tmpdir(), 'palette-primary-drive-'))) throw new Error('Unexpected cleanup root')
    await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
  }
})
