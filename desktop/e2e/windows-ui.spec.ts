import { test, expect, _electron as electron, type ElectronApplication, type Page } from '@playwright/test'
import { mkdtemp, rm, mkdir, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { execFileSync } from 'node:child_process'

async function checkHeader(page: Page): Promise<void> {
  const layout = await page.locator('.desktop-titlebar').evaluate(element => {
    const style = getComputedStyle(element)
    const indicator = element.lastElementChild!.getBoundingClientRect()
    return { height: element.getBoundingClientRect().height, paddingRight: parseFloat(style.paddingRight), indicatorRight: indicator.right, width: innerWidth, drag: style.getPropertyValue('-webkit-app-region') }
  })
  expect(layout.height).toBe(48)
  expect(layout.paddingRight).toBeGreaterThan(100)
  expect(layout.indicatorRight).toBeLessThanOrEqual(layout.width - layout.paddingRight + 1)
  expect(layout.drag).toBe('drag')
  await expect(page.locator('.window-drag-region')).toBeHidden()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
}

async function checkDialog(page: Page): Promise<void> {
  const dialog = page.getByRole('dialog').last()
  const bounds = await dialog.evaluate(element => {
    const rect = element.getBoundingClientRect()
    return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, width: innerWidth, height: innerHeight }
  })
  expect(bounds.top).toBeGreaterThanOrEqual(31)
  expect(bounds.bottom).toBeLessThanOrEqual(bounds.height - 31)
  expect(bounds.left).toBeGreaterThanOrEqual(15)
  expect(bounds.right).toBeLessThanOrEqual(bounds.width - 15)
}

async function renderedFonts(application: ElectronApplication): Promise<{ familyName: string; glyphCount: number }[]> {
  return application.evaluate(async ({ BrowserWindow }) => {
    const protocol = BrowserWindow.getAllWindows()[0].webContents.debugger
    protocol.attach('1.3')
    try {
      await protocol.sendCommand('DOM.enable')
      await protocol.sendCommand('CSS.enable')
      const { root } = await protocol.sendCommand('DOM.getDocument')
      const { nodeId } = await protocol.sendCommand('DOM.querySelector', { nodeId: root.nodeId, selector: 'h1' })
      const { fonts } = await protocol.sendCommand('CSS.getPlatformFontsForNode', { nodeId })
      return fonts.map((font: { familyName: string; glyphCount: number }) => ({ familyName: font.familyName, glyphCount: font.glyphCount }))
    } finally { protocol.detach() }
  })
}

async function checkNativeBackdrop(application: ElectronApplication, color: string): Promise<void> {
  await expect.poll(() => application.evaluate(() => {
    const state = globalThis as typeof globalThis & { nativeOverlayColor?: string }
    return state.nativeOverlayColor ?? '#f7f6f5'
  })).toBe(color)
}

for (const scale of [1, 1.25, 1.5, 2]) {
  test(`Windows title bar, Chinese fonts and dialogs at ${scale * 100}%`, async () => {
    test.skip(process.platform !== 'win32', 'Native Windows overlay and fonts require Windows')
    test.setTimeout(120_000)
    const repository = resolve('..')
    const root = await mkdtemp(join(tmpdir(), 'palette-windows-ui-'))
    const uv = process.env.PALETTE_UV ?? 'uv'
    execFileSync(uv, ['run', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
    const application = await electron.launch({ args: [`--force-device-scale-factor=${scale}`, resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
    const artifacts = resolve('test-results', `windows-${scale * 100}`)
    await mkdir(artifacts, { recursive: true })
    try {
      const page = await application.firstWindow()
      await application.evaluate(({ BrowserWindow }) => {
        const window = BrowserWindow.getAllWindows()[0]
        const setOverlay = window.setTitleBarOverlay.bind(window)
        window.setTitleBarOverlay = options => {
          setOverlay(options)
          const state = globalThis as typeof globalThis & { nativeOverlayColor?: string }
          state.nativeOverlayColor = options.color
        }
      })
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await expect(page.getByRole('button', { name: '后台已连接', exact: true })).toBeVisible({ timeout: 30_000 })
      expect(await page.evaluate(() => window.palette.platform)).toBe('win32')
      expect(await page.evaluate(() => window.palette.setWindowBackdrop(NaN).then(() => false, () => true))).toBe(true)
      expect(await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isMenuBarVisible())).toBe(false)
      await page.evaluate(async () => { await document.fonts.ready })
      const fonts = await renderedFonts(application)
      expect(fonts.some(font => /Microsoft YaHei/i.test(font.familyName) && font.glyphCount > 0)).toBe(true)
      await writeFile(join(artifacts, 'fonts.json'), JSON.stringify(fonts, null, 2))
      expect(await page.evaluate(() => devicePixelRatio)).toBeCloseTo(scale)
      for (const [width, height] of [[1440, 900], [1100, 700]]) {
        await application.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(size[0], size[1]), [width, height])
        // Native borders can round to one logical pixel at fractional/high DPI.
        await page.waitForFunction(width => Math.abs(innerWidth - width) <= 1, width)
        await checkHeader(page)
        await page.screenshot({ path: join(artifacts, `empty-${width}.png`) })
        await page.getByRole('button', { name: '收起侧栏' }).click()
        await expect(page.locator('.app-sidebar')).toBeHidden()
        await checkHeader(page)
        expect(await page.locator('.desktop-titlebar').evaluate(element => getComputedStyle(element).paddingLeft)).toBe('12px')
        await page.getByRole('button', { name: '展开侧栏' }).click()
      }
      await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].maximize())
      await expect.poll(() => application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isMaximized())).toBe(true)
      await checkHeader(page)
      await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].unmaximize())
      await expect.poll(() => application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isMaximized())).toBe(false)
      await page.waitForFunction(() => Math.abs(innerWidth - 1100) <= 1)

      await page.getByRole('button', { name: '设置', exact: true }).click()
      await checkHeader(page)
      await page.screenshot({ path: join(artifacts, 'environment-960.png') })
      await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
      await application.evaluate(({ dialog }, path) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [path] }) }, join(root, 'base.qcow2'))
      await page.getByRole('button', { name: '创建虚拟机', exact: true }).click()
      const wizard = page.getByRole('dialog')
      await checkDialog(page)
      await checkNativeBackdrop(application, '#b2b1b0')
      await wizard.getByRole('button', { name: '浏览', exact: true }).click()
      await wizard.getByRole('button', { name: '下一步', exact: true }).click()
      await wizard.getByLabel('名称', { exact: true }).fill('Windows 高 DPI 开发环境')
      await wizard.getByRole('button', { name: '更多硬件选项', exact: true }).click()
      await wizard.getByRole('button', { name: '网络', exact: true }).click()
      await checkDialog(page)
      await wizard.getByRole('button', { name: '下一步', exact: true }).scrollIntoViewIfNeeded()
      await page.screenshot({ path: join(artifacts, 'create-960.png') })
      await wizard.getByRole('button', { name: '下一步', exact: true }).click()
      await wizard.getByRole('button', { name: '创建虚拟机', exact: true }).click()
      await expect(page.getByRole('button', { name: '虚拟机设置', exact: true })).toBeVisible()
      await checkNativeBackdrop(application, '#f7f6f5')
      await page.getByRole('button', { name: '虚拟机设置', exact: true }).click()
      await expect(page.getByLabel('设置分类')).toBeVisible()
      await checkNativeBackdrop(application, '#b2b1b0')
      await page.getByLabel('设置分类').getByRole('button', { name: '网络', exact: true }).click()
      await checkDialog(page)
      const settings = page.getByRole('dialog')
      for (let index = 0; index < 6; index++) await settings.getByRole('button', { name: '添加', exact: true }).click()
      const scroll = await page.locator('.vm-settings-body').evaluate(element => {
        element.scrollTop = element.scrollHeight
        return { height: element.clientHeight, scrollHeight: element.scrollHeight, scrollTop: element.scrollTop }
      })
      expect(scroll.scrollHeight).toBeGreaterThan(scroll.height)
      expect(scroll.scrollTop).toBeGreaterThan(0)
      await checkDialog(page)
      const save = await settings.getByRole('button', { name: '保存', exact: true }).boundingBox()
      expect(save).not.toBeNull()
      expect(save!.y + save!.height).toBeLessThan(await page.evaluate(() => innerHeight - 31))
      await page.screenshot({ path: join(artifacts, 'hardware-960.png') })
      await settings.getByRole('button', { name: '取消', exact: true }).click()
      await expect(page.getByRole('heading', { name: '保存配置修改？' })).toBeVisible()
      await checkNativeBackdrop(application, '#80807f')
      await page.getByRole('button', { name: '继续编辑', exact: true }).click()
      await checkNativeBackdrop(application, '#b2b1b0')
      await settings.getByRole('button', { name: '取消', exact: true }).click()
      await page.getByRole('button', { name: '丢弃修改', exact: true }).click()
      await checkNativeBackdrop(application, '#f7f6f5')
      await page.getByRole('button', { name: '返回虚拟机列表' }).click()
      await page.screenshot({ path: join(artifacts, 'list-960.png') })
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      expect(errors).toEqual([])
      expect(await page.locator('body').innerText()).not.toMatch(/prompt|Cashmere|Electron|TypeScript|设计系统/i)
    } finally {
      await application.evaluate(({ app }) => app.exit(0)).catch(() => {})
      await application.close().catch(() => {})
      try { execFileSync(uv, ['run', 'palette', '--root', root, 'daemon', 'stop'], { cwd: repository }) } catch { /* Owner may already be stopped. */ }
      await rm(root, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 })
    }
  })
}
