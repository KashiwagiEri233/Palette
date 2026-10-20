import { test, expect, _electron as electron } from '@playwright/test'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { execFileSync } from 'node:child_process'

test('OS presets precede sources, preserve resource edits and persist both guest architectures', async () => {
  test.setTimeout(120000)
  const repository = resolve('..')
  const root = await mkdtemp(join(tmpdir(), 'palette-presets-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  execFileSync(uv, ['run', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  const original = await readFile(join(root, 'profiles/default.toml'), 'utf8')
  await writeFile(join(root, 'profiles/arm-test.toml'), original.replace('profile_id = "default"', 'profile_id = "arm-test"').replace('arch = "x86_64"', 'arch = "aarch64"').replace('machine = "q35"', 'machine = "virt"'))
  await writeFile(join(root, 'install.iso'), 'Disposable source selector fixture')
  const application = await electron.launch({ args: [resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  try {
    const page = await application.firstWindow()
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await expect(page.getByText('后台已连接')).toBeVisible({ timeout: 30000 })
    const cases = [
      { family: 'Windows', id: 'windows', arch: 'x86_64', source: 'image' },
      { family: 'Linux', id: 'linux', arch: 'ARM64', source: 'iso' },
      { family: 'Windows', id: 'windows', arch: 'ARM64', source: 'image' },
      { family: 'Linux', id: 'linux', arch: 'x86_64', source: 'iso' }
    ]
    for (const [index, item] of cases.entries()) {
      await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
      await page.getByRole('button', { name: '创建虚拟机', exact: true }).first().click()
      const wizard = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: '选择配置', exact: true }) })
      await expect(wizard.getByRole('button', { name: '浏览', exact: true })).toHaveCount(0)
      await wizard.getByRole('button', { name: `${item.family} 预设`, exact: true }).click()
      await wizard.getByRole('combobox', { name: '客户机架构', exact: true }).click()
      await page.getByRole('option', { name: item.arch, exact: true }).click()
      await expect(wizard.getByRole('button', { name: '下一步', exact: true })).toBeDisabled()
      await wizard.getByRole('checkbox', { name: '使用软件模拟（TCG，运行速度较慢）' }).check()
      if (!index) await page.screenshot({ path: 'test-results/os-presets-selection.png', animations: 'disabled' })
      await wizard.getByRole('button', { name: '下一步', exact: true }).click()
      const dialog = page.getByRole('dialog')
      await dialog.getByRole('button', { name: item.source === 'iso' ? 'ISO 安装' : '磁盘镜像', exact: true }).click()
      const path = join(root, item.source === 'iso' ? 'install.iso' : 'base.qcow2')
      await application.evaluate(({ dialog }, path) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path] }) }, path)
      await dialog.getByRole('button', { name: '浏览', exact: true }).click()
      await expect(dialog.getByLabel(item.source === 'iso' ? 'ISO 文件' : '镜像文件', { exact: true })).toHaveValue(path)
      if (item.source === 'image') await expect(dialog.getByRole('status')).toContainText('镜像虚拟容量')
      await application.evaluate(({ dialog }) => { dialog.showOpenDialog = async () => ({ canceled: true, filePaths: [] }) })
      await dialog.getByRole('button', { name: '浏览', exact: true }).click()
      await expect(dialog.getByLabel(item.source === 'iso' ? 'ISO 文件' : '镜像文件', { exact: true })).toHaveValue(path)
      await dialog.getByRole('button', { name: '下一步', exact: true }).click()
      await expect(dialog.getByLabel('CPU 核数')).toHaveValue(item.id === 'windows' ? '4' : '2')
      await expect(dialog.getByLabel('内存 / GiB')).toHaveValue(item.id === 'windows' ? '8' : '4')
      await expect(dialog.getByLabel('磁盘 / GiB')).toHaveValue(item.id === 'windows' ? '80' : '32')
      await dialog.getByLabel('名称', { exact: true }).fill(`预设-${index}`)
      await dialog.getByLabel('CPU 核数').fill('1')
      await dialog.getByLabel('内存 / GiB').fill('0.5')
      await dialog.getByLabel('磁盘 / GiB').fill('40')
      if (!index) {
        await dialog.getByRole('button', { name: '上一步', exact: true }).click()
        await dialog.getByRole('button', { name: '上一步', exact: true }).click()
        await dialog.getByRole('button', { name: 'Linux 预设', exact: true }).click()
        const confirmation = page.getByRole('dialog').filter({ has: page.getByRole('heading', { name: '更换配置？', exact: true }) })
        await expect(confirmation).toBeVisible()
        await confirmation.getByRole('button', { name: '继续编辑', exact: true }).click()
        await dialog.getByRole('button', { name: '下一步', exact: true }).click()
        await expect(dialog.getByLabel('镜像文件', { exact: true })).toHaveValue(path)
        await dialog.getByRole('button', { name: '下一步', exact: true }).click()
        await expect(dialog.getByLabel('CPU 核数')).toHaveValue('1')
        await expect(dialog.getByLabel('内存 / GiB')).toHaveValue('0.5')
        await expect(dialog.getByLabel('磁盘 / GiB')).toHaveValue('40')
        await page.screenshot({ path: 'test-results/os-presets-resources.png', animations: 'disabled' })
      }
      await dialog.getByRole('button', { name: '下一步', exact: true }).click()
      await expect(dialog.getByRole('heading', { name: '确认创建', exact: true })).toBeVisible()
      await expect(dialog.getByText('1 核 / 0.5 GiB', { exact: true })).toBeVisible()
      await dialog.getByRole('button', { name: '创建虚拟机', exact: true }).click()
      await expect(page.getByRole('heading', { name: `预设-${index}`, exact: true })).toBeVisible()
      const definition = await page.evaluate(async name => {
        const result = await window.palette.request('vms.list') as { vms: { name: string; definition: { draft: Record<string, unknown> }; status: { guest_arch: string } }[] }
        return result.vms.find(vm => vm.name === name)!
      }, `预设-${index}`)
      expect(definition.definition.draft).toMatchObject({ os_preset: item.id, vcpus: 1, memory_mib: 512, disk_gib: 40, source: item.source })
      expect(definition.status.guest_arch).toBe(item.arch === 'ARM64' ? 'aarch64' : 'x86_64')
    }
    expect(errors).toEqual([])
  } finally {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => undefined)
    await application.close().catch(() => undefined)
    execFileSync(uv, ['run', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository })
    await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
  }
})
