import { test, expect, _electron as electron } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdir, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { join, resolve, relative } from 'node:path'

test('real CPU display reaches Electron and retains controlled input', async ({}, testInfo) => {
  test.skip(!process.env.PALETTE_DBUS_QEMU || !process.env.PALETTE_DBUS_HELPER, 'Set PALETTE_DBUS_QEMU and PALETTE_DBUS_HELPER for real QEMU')
  test.setTimeout(120000)
  const repository = resolve('..'), parent = join(repository, '.runtime')
  const root = await mkdtemp(join(parent, 'desktop-raw-'))
  const qemu = resolve(process.env.PALETTE_DBUS_QEMU!), helper = resolve(process.env.PALETTE_DBUS_HELPER!)
  const uv = process.env.PALETTE_UV ?? 'uv'
  await mkdir(join(root, 'profiles'))
  const base = join(root, 'halt.raw')
  const bytes = Buffer.alloc(1024 * 1024)
  Buffer.from([0xfa, 0xf4, 0xeb, 0xfd]).copy(bytes)
  bytes[510] = 0x55; bytes[511] = 0xaa
  await writeFile(base, bytes)
  await writeFile(join(root, 'palette.toml'), `[host]\ndefault_profile = "default"\nruntime_root = ".runtime"\nexperimental_display = "dbus"\nexperimental_display_helper = ${JSON.stringify(helper)}\n`)
  await writeFile(join(root, 'profiles/default.toml'), `profile_id = "default"\n[qemu]\nbinary = ${JSON.stringify(qemu)}\naccelerator = "tcg"\ncpu = "max"\n[images]\nbase = ${JSON.stringify(base)}\nformat = "raw"\nsize_gib = 1\n[resources]\nvcpus = 1\nmemory_mib = 256\n[network]\nmode = "offline"\n[guest_tools]\nenabled = false\n`)
  const application = await electron.launch({ args: [resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  try {
    const page = await application.firstWindow()
    await expect(page.getByText('后台已连接', { exact: true })).toBeVisible({ timeout: 30000 })
    const vmId = await page.evaluate(async path => {
      const vm = await window.palette.request('vms.create', undefined, { name: '原始帧验收', profile_id: 'default', source: 'image', path, image_format: 'raw', vcpus: 1, memory_mib: 256, disk_gib: 1, network: 'offline' }) as { vm_id: string }
      await window.palette.request('vms.start', vm.vm_id)
      return vm.vm_id
    }, base)
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    await page.getByRole('button', { name: '原始帧验收', exact: true }).click()
    await page.getByRole('tab', { name: '控制台', exact: true }).click()
    const canvas = page.locator('.console-surface canvas')
    await expect(canvas).toHaveAttribute('data-palette-display', 'raw', { timeout: 30000 })
    await expect.poll(async () => canvas.evaluate(element => (element as HTMLCanvasElement).width)).toBeGreaterThan(0)
    await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].focus())
    await page.getByRole('button', { name: '接管', exact: true }).click()
    await expect(page.getByText('人工控制', { exact: true })).toBeVisible()
    await page.keyboard.press('a')
    const box = (await canvas.boundingBox())!
    await page.mouse.move(box.x + 10, box.y + 10); await page.mouse.down()
    await page.mouse.move(box.x + box.width + 20, box.y + box.height + 20, { steps: 20 }); await page.mouse.up()
    await page.getByRole('button', { name: '结束控制', exact: true }).click()
    await expect.poll(async () => page.evaluate(async vmId => {
      const { vms } = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { input_ledger: { keys: string[]; buttons: string[]; release_confirmed: boolean } } }[] }
      return vms.find(vm => vm.vm_id === vmId)!.status.input_ledger
    }, vmId)).toMatchObject({ keys: [], buttons: [], release_confirmed: true })
    const shadow = await page.evaluate(async vmId => {
      const { vms } = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { instance_id: string } }[] }
      return window.palette.request('display.shadow', vmId, { instance_id: vms.find(vm => vm.vm_id === vmId)!.status.instance_id })
    }, vmId)
    expect(shadow).toMatchObject({ authoritative: false })
    await page.screenshot({ path: testInfo.outputPath('raw-desktop.png') })
    await page.evaluate(async vmId => {
      const { vms } = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { instance_id: string } }[] }
      await window.palette.request('vms.stop', vmId, { force: true, expected_instance_id: vms.find(vm => vm.vm_id === vmId)!.status.instance_id })
    }, vmId)
  } finally {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => {})
    await application.close().catch(() => {})
    // Resolve and verify the exact owned temp directory before recursive cleanup.
    const owned = relative(parent, root)
    if (!owned || owned.startsWith('..') || owned.includes('/') || owned.includes('\\')) throw new Error('Unexpected test root')
    execFileSync(uv, ['run', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository })
    await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
  }
})
