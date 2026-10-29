import { test, expect, _electron as electron } from '@playwright/test'
import { access, mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { execFileSync } from 'node:child_process'
import type { VmSettingsResult } from '../src/shared/types'

test('UTM deletion stays in the draft until Save and Cancel preserves every file', async () => {
  test.setTimeout(120000)
  const repository = resolve('..')
  const root = await mkdtemp(join(tmpdir(), 'palette-disk-delete-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  execFileSync(uv, ['run', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  const app = await electron.launch({ args: [resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  try {
    const page = await app.firstWindow()
    await expect(page.getByText('后台已连接')).toBeVisible({ timeout: 30000 })
    const vmId = await page.evaluate(async path => {
      const vm = await window.palette.request('vms.create', undefined, { name: '磁盘删除测试', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
      const result = await window.palette.request('vms.settings.get', vm.vm_id) as VmSettingsResult
      await window.palette.request('vms.update', vm.vm_id, { revision: result.revision, name: result.draft.name, vcpus: 1, memory_mib: 512, network: 'offline', iso_attached: false, settings: { ...result.settings, drives: [{ id: 'drive-delete', media: 'disk', size_gib: 1 }, { id: 'drive-import', media: 'disk', path, format: 'qcow2' }] } })
      return vm.vm_id
    }, join(root, 'base.qcow2'))
    const data = () => page.evaluate(async id => window.palette.request('vms.settings.get', id) as Promise<VmSettingsResult>, vmId)
    const first = await data()
    const path = first.managed_disks!['drive-delete'].path!
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    await page.getByRole('button', { name: '磁盘删除测试', exact: true }).click()
    const settings = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: '虚拟机设置', exact: true }) })
    const confirmation = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: '删除驱动器？', exact: true }) })
    const open = async () => { await page.getByRole('button', { name: '虚拟机设置', exact: true }).click(); await expect(settings.getByRole('navigation', { name: '设置分类' })).toBeVisible() }
    const remove = async () => { await settings.getByRole('button', { name: 'VirtIO 磁盘 1', exact: true }).click(); await settings.getByRole('button', { name: '删除驱动器…', exact: true }).click(); await expect(confirmation).toContainText('保存设置后'); await confirmation.getByRole('button', { name: '删除', exact: true }).click() }
    await open(); await remove()
    await access(path)
    expect((await data()).revision).toBe(first.revision)
    await settings.getByRole('button', { name: '取消', exact: true }).click()
    await page.getByRole('button', { name: '丢弃修改', exact: true }).click()
    await access(path)
    await open()
    await settings.getByLabel('名称', { exact: true }).fill('磁盘删除测试新名')
    await remove()
    await access(path)
    await page.screenshot({ path: 'test-results/utm-staged-delete.png', animations: 'disabled' })
    await settings.getByRole('button', { name: '保存', exact: true }).click()
    await expect(settings).toHaveCount(0)
    await expect(access(path)).rejects.toThrow()
    expect((await data()).draft.name).toBe('磁盘删除测试新名')
    await open()
    await settings.getByRole('button', { name: 'VirtIO 磁盘 1', exact: true }).click()
    await settings.getByRole('button', { name: '删除驱动器…', exact: true }).click()
    await expect(confirmation).toContainText('外部源文件与 ISO 保留')
    await confirmation.getByRole('button', { name: '删除', exact: true }).click()
    await settings.getByRole('button', { name: '保存', exact: true }).click()
    await expect(settings).toHaveCount(0)
    await access(join(root, 'base.qcow2'))
    expect((await data()).settings.drives).toHaveLength(0)
  } finally {
    await app.evaluate(({ app }) => app.exit(0)).catch(() => undefined)
    await app.close().catch(() => undefined)
    execFileSync(uv, ['run', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository })
    if (!resolve(root).startsWith(join(tmpdir(), 'palette-disk-delete-'))) throw new Error('Unexpected cleanup root')
    await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
  }
})
