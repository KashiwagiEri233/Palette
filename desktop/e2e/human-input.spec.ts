import { test, expect, _electron as electron } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

test('human input stays ordered, accumulates wheel motion and reports mock transport timings', async ({}, testInfo) => {
  test.setTimeout(120000)
  const repository = resolve('..')
  const root = await mkdtemp(join(tmpdir(), 'palette-human-input-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  execFileSync(uv, ['run', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  const application = await electron.launch({ args: [resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  try {
    const page = await application.firstWindow()
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.evaluate(() => {
      const samples: { queue_ms: number; roundtrip_ms: number }[] = []
      Object.assign(window, { humanInputSamples: samples })
      window.addEventListener('palette-input-timing', event => {
        samples.push((event as CustomEvent).detail)
        if (samples.length > 256) samples.shift()
      })
    })
    await expect(page.getByText('后台已连接')).toBeVisible({ timeout: 30000 })
    const vmId = await page.evaluate(async path => {
      const vm = await window.palette.request('vms.create', undefined, { name: '输入测试', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
      await window.palette.request('vms.start', vm.vm_id)
      return vm.vm_id
    }, join(root, 'base.qcow2'))
    await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
    await page.getByRole('button', { name: '输入测试', exact: true }).click()
    await page.getByRole('tab', { name: '控制台', exact: true }).click()
    const canvas = page.getByRole('tabpanel', { name: '控制台', exact: true }).locator('canvas')
    await expect(canvas).toBeVisible({ timeout: 20000 })
    await page.getByRole('combobox', { name: '画面缩放', exact: true }).click()
    await page.getByRole('option', { name: '原始尺寸', exact: true }).click()
    await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].focus())
    await page.getByRole('button', { name: '接管', exact: true }).click()
    await expect(page.getByText('人工控制', { exact: true })).toBeVisible()
    for (let i = 0; i < 160; i++) await page.keyboard.press('a')
    // Exceed the frame deadline while the independent heartbeat keeps the lease alive.
    await page.waitForTimeout(2500)
    await page.keyboard.press('a')
    await expect(page.getByText('人工控制', { exact: true })).toBeVisible()
    const box = (await canvas.boundingBox())!
    await page.mouse.move(box.x + 10, box.y + 10)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width + 20, box.y + box.height + 20, { steps: 30 })
    await page.mouse.up()
    await expect.poll(async () => page.evaluate(async vmId => {
      const result = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { input_ledger: { keys: string[]; buttons: string[] } } }[] }
      return result.vms.find(vm => vm.vm_id === vmId)!.status.input_ledger
    }, vmId)).toMatchObject({ keys: [], buttons: [] })
    const before = await page.evaluate(async vmId => (await window.palette.request('control.metrics', vmId) as { input: { qmp_ms: { count: number } } }).input.qmp_ms.count, vmId)
    await page.locator('.console-surface').evaluate(element => {
      for (let i = 0; i < 99; i++) element.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 1 }))
    })
    const afterSmall = await page.evaluate(async vmId => (await window.palette.request('control.metrics', vmId) as { input: { qmp_ms: { count: number } } }).input.qmp_ms.count, vmId)
    expect(afterSmall).toBe(before)
    await page.locator('.console-surface').evaluate(element => element.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 1 })))
    await expect.poll(async () => page.evaluate(async vmId => (await window.palette.request('control.metrics', vmId) as { input: { qmp_ms: { count: number } } }).input.qmp_ms.count, vmId)).toBe(before + 1)
    const report = await page.evaluate(async vmId => {
      const samples = (window as unknown as { humanInputSamples: { queue_ms: number; roundtrip_ms: number }[] }).humanInputSamples
      const percentile = (field: 'queue_ms' | 'roundtrip_ms', p: number): number => {
        const values = samples.map(sample => sample[field]).sort((a, b) => a - b)
        return values[Math.ceil(values.length * p / 100) - 1]
      }
      return {
        mode: 'mock', sample_count: samples.length,
        frontend: Object.fromEntries(['queue_ms', 'roundtrip_ms'].map(field => [field, Object.fromEntries([50, 95, 99].map(p => [`p${p}`, percentile(field as 'queue_ms' | 'roundtrip_ms', p)]))])),
        backend: await window.palette.request('control.metrics', vmId)
      }
    }, vmId)
    expect(report.sample_count).toBeGreaterThan(10)
    const path = testInfo.outputPath('human-input-mock.json')
    await writeFile(path, JSON.stringify(report, null, 2))
    await testInfo.attach('human-input-mock', { path, contentType: 'application/json' })
    await page.screenshot({ path: testInfo.outputPath('human-control.png') })
    // Reproduce a broken write in the actual Electron main process, before any bytes
    // reach the backend. The failed batch must never take the one-shot IPC path.
    await application.evaluate(() => {
      const { Socket } = process.getBuiltinModule('node:net') as typeof import('node:net')
      const original = Socket.prototype.write
      Socket.prototype.write = function (this: import('node:net').Socket, chunk: Uint8Array | string, ...args: unknown[]): boolean {
        if (Buffer.isBuffer(chunk) && chunk.includes('"method":"control.input"')) {
          Socket.prototype.write = original
          const callback = args.find(item => typeof item === 'function') as (error: Error) => void
          queueMicrotask(() => callback(Object.assign(new Error('write EPIPE'), { code: 'EPIPE' })))
          return false
        }
        return Reflect.apply(original, this, [chunk, ...args]) as boolean
      }
    })
    await page.keyboard.press('b')
    await expect(page.getByRole('alert')).toContainText('本批输入结果未知')
    await expect(page.getByText('人工控制', { exact: true })).toHaveCount(0)
    await page.getByRole('alert').getByRole('button', { name: '复制调试信息', exact: true }).click()
    await expect(page.getByRole('alert')).toContainText('已复制')
    expect(JSON.parse(await application.evaluate(({ clipboard }) => clipboard.readText()))).toMatchObject({ recentFailure: { operation: 'control.input', error: { code: 'ACTION_OUTCOME_UNKNOWN', details: { transport_error: { code: 'EPIPE' } } } } })
    await expect.poll(async () => page.evaluate(async vmId => {
      const { vms } = await window.palette.request('vms.list') as { vms: { vm_id: string; status: { control_state: string; input_ledger: { keys: string[]; buttons: string[] } } }[] }
      return vms.find(vm => vm.vm_id === vmId)!.status
    }, vmId)).toMatchObject({ control_state: 'available', input_ledger: { keys: [], buttons: [] } })
    await page.getByRole('alert').getByRole('button', { name: '关闭', exact: true }).click()
    await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].focus())
    await page.getByRole('button', { name: '接管', exact: true }).click()
    await expect(page.getByText('人工控制', { exact: true })).toBeVisible()
    await page.evaluate(vmId => {
      window.dispatchEvent(new CustomEvent('palette-control-lost', { detail: { vmId, leaseId: 'obsolete-lease', error: 'stale control notification' } }))
    }, vmId)
    await page.keyboard.press('b')
    await expect(page.getByRole('alert')).toHaveCount(0)
    await page.getByRole('button', { name: '结束控制', exact: true }).click()
    expect(errors).toEqual([])
  } finally {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => {})
    await application.close().catch(() => {})
    try { execFileSync(uv, ['run', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository }) } catch { /* Shutdown may already be complete. */ }
    await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
  }
})
