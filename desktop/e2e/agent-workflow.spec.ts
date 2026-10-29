import { test, expect, _electron as electron } from '@playwright/test'
import { createServer } from 'node:http'
import { execFileSync } from 'node:child_process'
import { mkdir, mkdtemp, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'

test('built-in pi configures a model, operates the VM, stops and restores history', async () => {
  test.setTimeout(240000)
  const repository = resolve('..')
  const temporary = join(repository, '.runtime', 'desktop-agent-tests')
  await mkdir(temporary, { recursive: true })
  const root = await mkdtemp(join(temporary, 'agent-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  execFileSync(uv, ['run', '--offline', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  let slow = false
  let rejectSchema = false
  let finishArguments: (() => void) | undefined
  let finishFinal: (() => void) | undefined
  let appendSlow: ((text: string) => void) | undefined
  const requestedTools = new Set<string>()
  const server = createServer(async (request, response) => {
    let body = ''
    for await (const data of request) body += data
    const payload = JSON.parse(body) as { messages: { role: string; content: unknown }[]; tools?: { function?: { name: string; parameters: unknown } }[] }
    const invalidItems = (value: unknown): boolean => {
      if (Array.isArray(value)) return value.some(invalidItems)
      if (!value || typeof value !== 'object') return false
      const node = value as Record<string, unknown>
      if ('items' in node && typeof node.items !== 'boolean' && (!node.items || typeof node.items !== 'object' || Array.isArray(node.items))) return true
      return Object.values(node).some(invalidItems)
    }
    const invalid = payload.tools?.find(tool => invalidItems(tool.function?.parameters))
    if (invalid || rejectSchema) {
      response.writeHead(400, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ error: { message: `Invalid schema for function '${invalid?.function?.name ?? 'app'}': items must be boolean or object e2e-secret` } }))
      return
    }
    response.writeHead(200, { 'content-type': 'text/event-stream' })
    const chunk = (delta: unknown, reason: string | null = null) => response.write(`data: ${JSON.stringify({ id: 'palette-test', object: 'chat.completion.chunk', created: 1, model: 'fixture', choices: [{ index: 0, delta, finish_reason: reason }], ...(reason === 'stop' ? { usage: { prompt_tokens: 2048, completion_tokens: 128, total_tokens: 2176 } } : {}) })}\n\n`)
    chunk({ role: 'assistant' })
    if (slow) {
      chunk({ content: '正在等待下一步\n\n' + Array.from({ length: 100 }, (_, index) => `流式段落 ${index + 1}：保留历史内容并跟随新输出。`).join('\n\n') })
      appendSlow = text => chunk({ content: text })
      return
    }
    const tools = payload.messages.filter(message => message.role === 'tool')
    if (tools.length < 4) {
      const name = ['todo_write', 'observe', 'act', 'todo_write'][tools.length]
      requestedTools.add(name)
      let parameters: Record<string, unknown> = {}
      if (name === 'todo_write') {
        const completed = tools.length === 3
        parameters = { todos: [{ content: '观察虚拟机画面', status: completed ? 'completed' : 'in_progress' }, { content: '单击目标并确认结果', status: completed ? 'completed' : 'pending' }] }
      } else if (name === 'act') {
        const content = tools.at(-1)!.content
        const text = typeof content === 'string' ? content : (content as { type: string; text?: string }[]).map(part => part.text ?? '').join('')
        const frame = JSON.parse(text).frame as { frame_id: string; instance_id: string }
        parameters = { frame_id: frame.frame_id, instance_id: frame.instance_id, action: { type: 'click', x: 10, y: 10 } }
      }
      if (!tools.length) {
        chunk({ reasoning_content: '先查看客户机画面，' })
        chunk({ reasoning_content: '再执行点击并确认结果。' })
        chunk({ content: '我会先检查画面，再完成操作。' })
        const args = JSON.stringify(parameters)
        chunk({ tool_calls: [{ index: 0, id: 'call-0', type: 'function', function: { name, arguments: args.slice(0, 10) } }] })
        await new Promise<void>(resolve => { finishArguments = resolve })
        chunk({ tool_calls: [{ index: 0, function: { arguments: args.slice(10) } }] })
      } else chunk({ tool_calls: [{ index: 0, id: `call-${tools.length}`, type: 'function', function: { name, arguments: JSON.stringify(parameters) } }] })
      chunk({}, 'tool_calls')
    } else {
      if (!finishFinal) await new Promise<void>(resolve => { finishFinal = resolve })
      chunk({ content: '## 已完成虚拟机视觉操作\n\n**操作结果**\n\n- 观察画面\n- 单击目标\n\n```json\n{"ok":true}\n```\n\n| 检查项 | 结果 |\n| --- | --- |\n| 视觉操作 | 通过 |\n\n[查看说明](https://example.com/docs)' })
      chunk({}, 'stop')
    }
    response.end('data: [DONE]\n\n')
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const port = (server.address() as { port: number }).port
  const userData = join(root, 'electron')
  const launch = () => electron.launch({ args: [`--user-data-dir=${userData}`, resolve('out/main/index.js')], env: { ...process.env, PALETTE_ROOT: root, PALETTE_UV: uv } })
  let application = await launch()
  try {
    let page = await application.firstWindow()
    await expect(page.getByText('后台已连接', { exact: true })).toBeVisible({ timeout: 30000 })
    const vmId = await page.evaluate(async path => {
      const result = await window.palette.request('vms.create', undefined, { name: 'Agent 验证', profile_id: 'default', source: 'image', path, vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }) as { vm_id: string }
      await window.palette.request('vms.start', result.vm_id)
      return result.vm_id
    }, join(root, 'base.qcow2'))
    await page.getByRole('button', { name: '设置', exact: true }).click()
    await page.getByRole('button', { name: 'Agent', exact: true }).click()
    await page.getByRole('button', { name: '添加提供商', exact: true }).click()
    await page.getByRole('tab', { name: '自定义 API', exact: true }).click()
    await page.getByLabel('提供商名称', { exact: true }).fill('视觉测试服务')
    await page.getByLabel('Base URL', { exact: true }).fill(`http://127.0.0.1:${port}/v1`)
    await page.getByLabel('API Key', { exact: true }).fill('e2e-secret')
    await page.getByRole('button', { name: '手动添加模型', exact: true }).click()
    await page.getByLabel('模型 ID', { exact: true }).fill('fixture')
    await page.getByText('高级设置', { exact: true }).click()
    await page.getByRole('switch', { name: '支持图像输入', exact: true }).click()
    await page.getByRole('button', { name: '保存提供商', exact: true }).click()
    await expect(page.getByText('提供商已保存', { exact: true })).toBeVisible()
    await page.evaluate(async () => {
      const settings = await window.palette.request('agent.settings.get', undefined, {})
      const provider = settings.providers.find(provider => provider.name === '视觉测试服务')!
      await window.palette.request('agent.settings.update', undefined, { configuration: { ...settings.configuration, providerId: provider.id, modelId: 'fixture' } })
      window.dispatchEvent(new Event('palette-agent-settings'))
    })
    await page.getByRole('button', { name: '主页', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Agent 验证', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Agent 验证', exact: true }).click()
    await page.getByRole('textbox', { name: '任务内容' }).fill('观察画面并单击一次')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    const processRegion = page.locator('.agent-process').first()
    const processTrigger = processRegion.locator(':scope > .process-trigger')
    await expect(processTrigger).toHaveAttribute('aria-expanded', 'false')
    await processTrigger.click()
    const preparing = processRegion.locator('.tool-status-preparing').first()
    await preparing.locator(':scope > .process-trigger').click()
    await expect(preparing).toContainText('参数（生成中）')
    await expect(preparing.locator('.tool-json')).toContainText('{"todos":[')
    finishArguments!()
    const progress = page.locator('.agent-todo-panel')
    await expect(progress).toContainText('已完成 2', { timeout: 30000 })
    await expect(progress.locator(':scope > .process-trigger')).toHaveAttribute('aria-expanded', 'false')
    await progress.locator(':scope > .process-trigger').click()
    await expect(progress.locator('[data-todo-status=completed]')).toHaveCount(2)
    await expect(processRegion.locator('.screenshot-preview')).toHaveCount(0)
    const reasoning = processRegion.locator('.agent-reasoning').first()
    await reasoning.locator(':scope > .process-trigger').click()
    await expect(reasoning.locator('.agent-message-markdown')).toContainText('先查看客户机画面，再执行点击并确认结果。')
    const observation = processRegion.locator('.agent-tool-row').filter({ hasText: '观察画面 · 已完成' }).first()
    await observation.locator(':scope > .process-trigger').click()
    await expect(observation.locator('.screenshot-preview img')).toBeVisible()
    await observation.getByRole('button', { name: '放大截图 1', exact: true }).click()
    await expect(page.getByRole('dialog', { name: '工具截图 1', exact: true })).toBeVisible()
    await page.keyboard.press('Escape')
    await page.evaluate(() => {
      const fixture = window as typeof window & { restoreScreenshotUrl?: typeof URL.createObjectURL }
      fixture.restoreScreenshotUrl = URL.createObjectURL
      URL.createObjectURL = () => { throw new Error('fixture screenshot load failure') }
    })
    const actionRow = processRegion.locator('.agent-tool-row').filter({ hasText: '操作虚拟机 · 已完成' }).first()
    await actionRow.locator(':scope > .process-trigger').click()
    await expect(actionRow.getByRole('button', { name: '重试加载截图', exact: true })).toBeVisible()
    await page.evaluate(() => { URL.createObjectURL = (window as typeof window & { restoreScreenshotUrl: typeof URL.createObjectURL }).restoreScreenshotUrl })
    await actionRow.getByRole('button', { name: '重试加载截图', exact: true }).click()
    await expect(actionRow.locator('.screenshot-preview img')).toBeVisible()
    await actionRow.locator(':scope > .process-trigger').click()
    const lastTodo = processRegion.locator('.agent-tool-row').filter({ hasText: '更新任务进度' }).last()
    await lastTodo.locator(':scope > .process-trigger').click()
    await expect(lastTodo).toContainText('进度变化')
    await expect(lastTodo).toContainText('待开始 → 已完成')
    await page.screenshot({ path: 'test-results/pi-agent-process.png', animations: 'disabled' })
    await expect.poll(() => typeof finishFinal).toBe('function')
    finishFinal!()
    await expect(page.getByText('已完成虚拟机视觉操作', { exact: true })).toBeVisible({ timeout: 30000 })
    await expect(processTrigger).toHaveAttribute('aria-expanded', 'true')
    await expect(page.getByRole('status').filter({ hasText: '已完成 · 2 次工具调用' })).toBeVisible()
    expect([...requestedTools].sort()).toEqual(['act', 'observe', 'todo_write'])
    const contextTrigger = page.getByRole('button', { name: '上下文使用状况：已用 2%', exact: true })
    const modelSelect = page.getByRole('button', { name: '选择模型', exact: true })
    await expect(contextTrigger).toBeVisible()
    const contextBounds = await contextTrigger.boundingBox()
    const modelBounds = await modelSelect.boundingBox()
    const sendBounds = await page.getByRole('button', { name: '发送', exact: true }).boundingBox()
    expect(contextBounds!.x + contextBounds!.width).toBeLessThanOrEqual(modelBounds!.x)
    expect(modelBounds!.x + modelBounds!.width).toBeLessThanOrEqual(sendBounds!.x)
    await expect(page.getByRole('button', { name: '压缩上下文', exact: true })).toHaveCount(0)
    await contextTrigger.click()
    await expect(page.getByText('2% 已用（剩余 98%）', { exact: true })).toBeVisible()
    await expect(page.getByText('已用 2.2K / 128K tokens（估算）', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '压缩上下文', exact: true })).toBeEnabled()
    const fonts = await page.evaluate(async () => {
      await document.fonts.ready
      return { body: getComputedStyle(document.body).fontFamily, heading: getComputedStyle(document.querySelector('.agent-message-markdown h2')!).fontFamily, loaded: document.fonts.check('400 12px Inter') }
    })
    expect(fonts.body).toContain('Inter')
    expect(fonts.heading).toContain('Inter')
    expect(fonts.loaded).toBe(true)
    await page.screenshot({ path: 'test-results/pi-agent-context.png', animations: 'disabled' })
    await page.evaluate(() => document.documentElement.classList.add('dark'))
    await page.screenshot({ path: 'test-results/pi-agent-context-dark.png', animations: 'disabled' })
    await page.evaluate(() => document.documentElement.classList.remove('dark'))
    await page.keyboard.press('Escape')
    await page.getByRole('textbox', { name: '任务内容' }).hover()
    const originalSize = await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getContentSize())
    await application.evaluate(({ BrowserWindow }) => { const window = BrowserWindow.getAllWindows()[0]; window.setMinimumSize(600, 640); window.setContentSize(960, 800) })
    await expect(page.locator('.conversation-page')).toHaveAttribute('data-workspace-mode', 'drawer')
    await expect(modelSelect).toBeVisible()
    await expect(contextTrigger).toBeVisible()
    await contextTrigger.click()
    await expect(page.getByRole('button', { name: '压缩上下文', exact: true })).toBeVisible()
    await page.screenshot({ path: 'test-results/pi-agent-context-narrow.png', animations: 'disabled' })
    await page.keyboard.press('Escape')
    await page.getByRole('textbox', { name: '任务内容' }).hover()
    await application.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(size[0], size[1]), originalSize)
    const originalSettings = await page.evaluate(() => window.palette.request('agent.settings.get', undefined, {}))
    await modelSelect.click()
    const modelMenu = page.getByRole('dialog', { name: '模型与推理等级', exact: true })
    await expect(modelMenu.getByRole('button', { name: '推理等级 Off', exact: true })).toBeDisabled()
    await page.screenshot({ path: 'test-results/model-selector-summary.png', animations: 'disabled' })
    await modelMenu.getByRole('button', { name: '模型 fixture', exact: true }).click()
    const modelList = page.getByRole('dialog', { name: '选择提供商和模型', exact: true })
    const modelSearch = modelList.getByRole('combobox', { name: '搜索模型或提供商', exact: true })
    await expect(modelSearch).toBeFocused()
    await expect(modelList.getByRole('option', { name: 'fixture 当前模型', exact: true })).toBeVisible()
    await modelSearch.fill('no-such-model-987654')
    await expect(modelList.getByText('没有找到匹配的模型')).toBeVisible()
    await expect(modelList.getByRole('option')).toHaveCount(0)
    await modelSearch.fill('openai')
    await expect(modelList.getByRole('option')).toHaveCount(0)
    await page.evaluate(async () => {
      await window.palette.request('agent.credentials.set', undefined, { providerId: 'openai', key: 'unused-e2e-catalog-key' })
      window.dispatchEvent(new Event('palette-agent-settings'))
    })
    // Pick a reasoning model through its configured provider group and verify metadata.
    const targetModel = originalSettings.models.find(model => model.providerId === 'openai' && model.reasoning)!
    expect(targetModel).toBeTruthy()
    await modelSearch.fill(targetModel.providerId)
    await expect(modelList.getByRole('group', { name: targetModel.providerId, exact: true })).toBeVisible()
    await expect(modelList.getByRole('group', { name: 'anthropic', exact: true })).toBeHidden()
    await page.screenshot({ path: 'test-results/model-selector-models.png', animations: 'disabled' })
    await page.evaluate(() => document.documentElement.classList.add('dark'))
    await page.screenshot({ path: 'test-results/model-selector-models-dark.png', animations: 'disabled' })
    await page.evaluate(() => document.documentElement.classList.remove('dark'))
    await modelList.getByRole('group', { name: targetModel.providerId, exact: true }).getByRole('option', { name: targetModel.name, exact: true }).click()
    await expect(modelList).toBeHidden()
    await expect(modelSelect).toContainText(targetModel.name)
    const selectedSettings = await page.evaluate(() => window.palette.request('agent.settings.get', undefined, {}))
    expect(selectedSettings.configuration).toMatchObject({ providerId: targetModel.providerId, modelId: targetModel.id, api: targetModel.api, baseUrl: targetModel.baseUrl.replace(/\/$/, ''), vision: targetModel.vision, reasoning: true, custom: false, maxSteps: originalSettings.configuration.maxSteps })
    await modelSelect.click()
    await modelMenu.getByRole('button', { name: /^推理等级/ }).click()
    const effortMenu = page.getByRole('dialog', { name: '选择推理等级', exact: true })
    await expect(effortMenu.getByRole('option')).toHaveCount(7)
    await page.screenshot({ path: 'test-results/model-selector-effort.png', animations: 'disabled' })
    await page.keyboard.press('End')
    await page.keyboard.press('Enter')
    await expect(effortMenu).toBeHidden()
    await expect(modelSelect).toContainText('Max')
    expect((await page.evaluate(() => window.palette.request('agent.settings.get', undefined, {}))).configuration.thinkingLevel).toBe('max')
    await application.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(960, 800))
    await expect(page.locator('.conversation-page')).toHaveAttribute('data-workspace-mode', 'drawer')
    await modelSelect.click()
    await modelMenu.getByRole('button', { name: /^模型 / }).click()
    await modelSearch.fill(targetModel.name)
    await expect(modelList.getByRole('group', { name: targetModel.providerId, exact: true }).getByLabel('当前模型', { exact: true })).toBeVisible()
    await page.screenshot({ path: 'test-results/model-selector-narrow.png', animations: 'disabled' })
    const menuBounds = await modelList.boundingBox()
    expect(menuBounds!.x).toBeGreaterThanOrEqual(0)
    expect(menuBounds!.y).toBeGreaterThanOrEqual(0)
    expect(menuBounds!.x + menuBounds!.width).toBeLessThanOrEqual((await page.evaluate(() => innerWidth)))
    await page.keyboard.press('Escape')
    await expect(modelSelect).toBeFocused()
    await application.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(size[0], size[1]), originalSize)
    await page.evaluate(async configuration => {
      await window.palette.request('agent.settings.update', undefined, { configuration })
      window.dispatchEvent(new Event('palette-agent-settings'))
    }, originalSettings.configuration)
    await expect(modelSelect).toContainText('fixture')
    const reply = page.locator('[data-message-role=assistant] .agent-message-markdown').last()
    await expect(reply.getByRole('heading', { name: '已完成虚拟机视觉操作', level: 2 })).toBeVisible()
    await expect(reply.locator('strong')).toHaveText('操作结果')
    await expect(reply.getByRole('listitem')).toHaveCount(2)
    await expect(reply.locator('pre code.language-json')).toContainText('{"ok":true}')
    await reply.getByRole('button', { name: '复制代码', exact: true }).click()
    await expect(reply.getByText('已复制', { exact: true })).toBeVisible()
    expect(await application.evaluate(({ clipboard }) => clipboard.readText())).toBe('{"ok":true}\n')
    await application.evaluate(({ clipboard }) => {
      const fixture = globalThis as typeof globalThis & { restoreCodeClipboard?: typeof clipboard.writeText }
      fixture.restoreCodeClipboard = clipboard.writeText
      clipboard.writeText = () => { throw new Error('fixture clipboard failure') }
    })
    await reply.getByRole('button', { name: '复制代码', exact: true }).click()
    await expect(reply.getByText('复制失败，重试', { exact: true })).toBeVisible()
    await application.evaluate(({ clipboard }) => { clipboard.writeText = (globalThis as typeof globalThis & { restoreCodeClipboard: typeof clipboard.writeText }).restoreCodeClipboard })
    await expect(reply.getByRole('table')).toBeVisible()
    await expect(page.getByRole('button', { name: '会话分支', exact: true })).toHaveCount(0)
    await expect(page.getByRole('dialog', { name: '会话分支', exact: true })).toHaveCount(0)
    await application.evaluate(({ shell }) => {
      shell.openExternal = async url => { (globalThis as typeof globalThis & { openedMarkdownUrl?: string }).openedMarkdownUrl = url }
    })
    await reply.getByRole('link', { name: '查看说明', exact: true }).click()
    expect(await application.evaluate(() => (globalThis as typeof globalThis & { openedMarkdownUrl?: string }).openedMarkdownUrl)).toBe('https://example.com/docs')
    for (const url of ['file:///etc/passwd', 'javascript:alert(1)']) {
      expect(await page.evaluate(url => window.palette.request('window.command', undefined, { action: 'open_external', url }).then(() => false, () => true), url)).toBe(true)
    }
    await processTrigger.click()
    await progress.locator(':scope > .process-trigger').click()
    await page.screenshot({ path: 'test-results/pi-agent-workflow.png' })
    await page.evaluate(() => document.documentElement.classList.add('dark'))
    await page.screenshot({ path: 'test-results/pi-agent-markdown-dark.png', animations: 'disabled' })
    await page.evaluate(() => document.documentElement.classList.remove('dark'))
    await processTrigger.click()
    await progress.locator(':scope > .process-trigger').click()
    const savedScroll = page.locator('.conversation-transcript [data-slot=scroll-area-viewport]')
    await savedScroll.evaluate(element => { element.scrollTop = 0 })
    await expect.poll(() => savedScroll.evaluate(element => element.scrollTop)).toBe(0)
    await page.getByRole('button', { name: '主页', exact: true }).click()
    await page.getByRole('button', { name: '观察画面并单击一次', exact: true }).click()
    await expect(processTrigger).toHaveAttribute('aria-expanded', 'true')
    await expect.poll(() => savedScroll.evaluate(element => element.scrollTop)).toBeLessThan(5)
    await expect(page.getByRole('button', { name: '有新消息 ↓', exact: true })).toHaveCount(0)
    rejectSchema = true
    await page.getByRole('textbox', { name: '任务内容' }).fill('debug-private-task')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    const runError = page.locator('.agent-run-error')
    await expect(runError).toContainText('工具参数格式不兼容', { timeout: 20000 })
    await expect(page.locator('.agent-todo-panel')).toHaveCount(0)
    await runError.getByRole('button', { name: '复制调试信息', exact: true }).click()
    await expect(runError).toContainText('已复制')
    const debugText = await application.evaluate(({ clipboard }) => clipboard.readText())
    expect(JSON.parse(debugText)).toMatchObject({ source: 'agent', vmId, model: { modelId: 'fixture', api: 'openai-completions' }, run: { state: 'failed' }, modelError: expect.stringContaining('Invalid schema') })
    expect(debugText).not.toContain('e2e-secret')
    expect(debugText).not.toContain('debug-private-task')
    await page.screenshot({ path: 'test-results/agent-error-debug-info.png', animations: 'disabled' })
    rejectSchema = false
    slow = true
    await page.getByRole('textbox', { name: '任务内容' }).fill('等待任务')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await expect(page.getByRole('button', { name: '停止 Agent', exact: true })).toBeVisible()
    await expect(page.locator('.agent-answer').last()).toContainText('流式段落 100')
    const scroll = page.locator('.conversation-transcript [data-slot=scroll-area-viewport]')
    await scroll.evaluate(element => { element.scrollTop = 0 })
    await expect.poll(() => scroll.evaluate(element => element.scrollTop)).toBe(0)
    appendSlow!('\n\n新的流式输出，不应打断向上阅读。')
    await expect(page.getByRole('button', { name: '有新消息 ↓', exact: true })).toBeVisible()
    expect(await scroll.evaluate(element => element.scrollTop)).toBeLessThan(5)
    await page.getByRole('button', { name: '有新消息 ↓', exact: true }).click()
    await expect.poll(() => scroll.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(5)
    await expect(modelSelect).toBeDisabled()
    await page.getByRole('button', { name: /上下文使用状况/ }).click()
    await expect(page.getByRole('button', { name: '压缩上下文', exact: true })).toBeDisabled()
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: '停止 Agent', exact: true }).click()
    await expect(page.getByRole('status').filter({ hasText: '已停止' })).toBeVisible({ timeout: 10000 })
    await expect(page.locator('.agent-answer').last()).toContainText('新的流式输出')
    await page.evaluate(() => window.palette.finishClose('background'))
    await application.close()
    application = await launch()
    page = await application.firstWindow()
    await expect(page.getByText('后台已连接', { exact: true })).toBeVisible({ timeout: 30000 })
    await page.getByRole('button', { name: '观察画面并单击一次', exact: true }).click()
    await expect(page.getByText('已完成虚拟机视觉操作', { exact: true })).toBeVisible()
    await expect(page.locator('.agent-answer').last()).toContainText('新的流式输出')
    const restoredProcess = page.locator('.agent-process').first()
    await expect(restoredProcess.locator(':scope > .process-trigger')).toHaveAttribute('aria-expanded', 'false')
    await restoredProcess.locator(':scope > .process-trigger').click()
    await restoredProcess.locator('.agent-tool-row').filter({ hasText: '观察画面 · 已完成' }).first().locator(':scope > .process-trigger').click()
    await expect(restoredProcess.locator('.screenshot-preview img').first()).toBeVisible()
    await expect(page.getByRole('status').filter({ hasText: '已停止' })).toBeVisible()
    await page.evaluate(async id => { await window.palette.request('vms.stop', id); await window.palette.finishClose('shutdown') }, vmId)
  } finally {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => {})
    await application.close().catch(() => {})
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
    try { execFileSync(uv, ['run', '--offline', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository, stdio: 'ignore' }) } catch { /* It may have stopped through the UI. */ }
    await rm(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 })
  }
})
