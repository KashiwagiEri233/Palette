import { test, expect, _electron as electron } from '@playwright/test'
import { mkdtemp, mkdir, writeFile, readFile, rm, rename } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { execFileSync } from 'node:child_process'
import type { Menu, MessageBoxOptions } from 'electron'

test('Windows tools mount from the VM menu and application menu without replacing installation media', async () => {
  test.setTimeout(120000)
  const repository = resolve('..')
  const root = await mkdtemp(join(tmpdir(), 'palette-tools-media-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  execFileSync(uv, ['run', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  const directory = join(root, 'images/guest-tools')
  await mkdir(directory, { recursive: true })
  const fixture = Buffer.alloc(32768)
  const media = join(directory, 'palette-tools-windows-x86_64.iso')
  await writeFile(media, fixture)
  await writeFile(media.replace('.iso', '.json'), JSON.stringify({ schema_version: 1, guest_os: 'windows', arch: 'x86_64', version: 'test', sha256: createHash('sha256').update(fixture).digest('hex'), size_bytes: fixture.length }))
  const installer = join(root, 'system.iso')
  await writeFile(installer, 'system media fixture')
  const application = await electron.launch({ args: [resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  try {
    const page = await application.firstWindow()
    await expect(page.getByText('后台已连接')).toBeVisible({ timeout: 30000 })
    const vm = await page.evaluate(async path => window.palette.request('vms.create', undefined, { name: 'Windows 工具测试', os_preset: 'windows', profile_id: 'default', source: 'iso', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as Promise<{ vm_id: string }>, installer)
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    await page.getByRole('button', { name: 'Windows 工具测试', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Windows 工具测试', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Windows 工具测试 的操作' }).click()
    await expect(page.getByRole('menuitem', { name: '安装增强工具与驱动', exact: true })).toBeEnabled()
    await page.screenshot({ path: 'test-results/windows-tools-menu.png', animations: 'disabled' })
    await page.getByRole('menuitem', { name: '安装增强工具与驱动', exact: true }).click()
    await expect(page.getByRole('dialog')).toContainText('将在下次启动时装载')
    await page.getByRole('dialog').getByRole('button', { name: '知道了' }).click()
    const definitionPath = join(root, '.runtime/definitions', vm.vm_id + '.json')
    let definition = JSON.parse(await readFile(definitionPath, 'utf8'))
    expect(definition.tools_iso_sha256).toHaveLength(64)
    expect(definition.draft.path).toBe(installer)
    expect(definition.draft.settings.drives).toEqual([expect.objectContaining({ media: 'cdrom', path: installer, attached: true })])
    await page.getByRole('button', { name: '启动', exact: true }).click()
    await expect(page.getByText('仅查看', { exact: true })).toBeVisible({ timeout: 20000 })
    await page.getByRole('button', { name: 'Windows 工具测试 的操作' }).click()
    await page.getByRole('menuitem', { name: '弹出增强工具光盘', exact: true }).click()
    await expect.poll(async () => JSON.parse(await readFile(definitionPath, 'utf8')).tools_iso_sha256).toBeNull()
    // Exercise the actual native application menu callback while suppressing only popup UI.
    await application.evaluate(({ Menu, dialog }) => {
      const state = globalThis as typeof globalThis & { toolsMenu?: Menu; toolsMessage?: MessageBoxOptions }
      const build = Menu.buildFromTemplate.bind(Menu)
      Menu.buildFromTemplate = template => { const menu = build(template); menu.popup = () => {}; state.toolsMenu = menu; return menu }
      dialog.showMessageBox = async (...args) => { state.toolsMessage = args.at(-1) as MessageBoxOptions; return { response: 0, checkboxChecked: false } }
    })
    await page.getByRole('button', { name: '应用菜单', exact: true }).click()
    await expect.poll(async () => application.evaluate(() => (globalThis as typeof globalThis & { toolsMenu?: Menu }).toolsMenu?.getMenuItemById('install-windows-tools')?.enabled)).toBe(true)
    await application.evaluate(() => {
      const item = (globalThis as typeof globalThis & { toolsMenu: Menu }).toolsMenu.getMenuItemById('install-windows-tools')!
      ;(item.click as unknown as () => void)()
    })
    await expect.poll(async () => application.evaluate(() => (globalThis as typeof globalThis & { toolsMessage?: MessageBoxOptions }).toolsMessage?.message)).toBe('增强工具光盘已装载')
    await page.getByRole('tab', { name: '配置', exact: true }).click()
    const toolsState = page.getByText('增强工具光盘', { exact: true }).locator('..')
    await expect(toolsState).toContainText('已装载')
    definition = JSON.parse(await readFile(definitionPath, 'utf8'))
    expect(definition.tools_iso_sha256).toHaveLength(64)
    expect(definition.draft.path).toBe(installer)
    const log = await readFile(join(root, '.runtime/vms', vm.vm_id, definition.storage_owner, 'disk/overlay.qcow2.log.jsonl'), 'utf8')
    const commands = log.trim().split('\n').map(line => JSON.parse(line))
    expect(commands.filter(item => item.command === 'blockdev-change-medium')).toHaveLength(1)
    expect(commands.find(item => item.command === 'blockdev-change-medium').arguments.id).toContain('palette-tools-cd')
    const response = await page.evaluate(async () => window.palette.request('vms.list') as Promise<{ vms: { vm_id: string; status: { instance_id: string } }[] }>)
    await page.evaluate(async ({ id, revision, instance }) => window.palette.request('vms.tools.eject', id, { revision, instance_id: instance }), { id: vm.vm_id, revision: definition.revision, instance: response.vms.find(item => item.vm_id === vm.vm_id)!.status.instance_id })
    await expect(toolsState).toContainText('未装载')
    // A corrupt ISO fails before a medium-change command can reach the guest.
    await writeFile(media, Buffer.alloc(32768, 1))
    await page.getByRole('button', { name: 'Windows 工具测试 的操作' }).click()
    await page.getByRole('menuitem', { name: '安装增强工具与驱动', exact: true }).click()
    await expect(page.getByRole('alert')).toContainText('校验失败')
    // Losing backend discovery must disable VM operations without hiding application menus.
    const endpoint = join(root, '.runtime/.operator/endpoint.json')
    const heldEndpoint = join(root, '.runtime/.operator/endpoint.test-held.json')
    await rename(endpoint, heldEndpoint)
    try {
      await page.getByRole('button', { name: '应用菜单', exact: true }).click()
      await expect.poll(async () => application.evaluate(() => (globalThis as typeof globalThis & { toolsMenu?: Menu }).toolsMenu?.getMenuItemById('install-windows-tools')?.enabled)).toBe(false)
      expect(await application.evaluate(() => (globalThis as typeof globalThis & { toolsMenu: Menu }).toolsMenu.items.map(item => item.label))).toEqual(['文件', '虚拟机', '编辑', '视图', '帮助'])
    } finally { await rename(heldEndpoint, endpoint) }
  } finally {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => undefined)
    await application.close().catch(() => undefined)
    execFileSync(uv, ['run', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository })
    await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
  }
})
