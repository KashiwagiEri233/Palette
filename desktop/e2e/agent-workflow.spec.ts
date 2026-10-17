import { test, expect, _electron as electron } from '@playwright/test'
import { createServer } from 'node:http'
import { execFileSync } from 'node:child_process'
import { mkdir, mkdtemp, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'

test('built-in pi configures a model, operates the VM, stops and restores history', async () => {
  test.setTimeout(180000)
  const repository = resolve('..')
  const temporary = join(repository, '.runtime', 'desktop-agent-tests')
  await mkdir(temporary, { recursive: true })
  const root = await mkdtemp(join(temporary, 'agent-'))
  const uv = process.env.PALETTE_UV ?? 'uv'
  execFileSync(uv, ['run', '--offline', 'python', resolve('scripts/setup_fixture.py'), root], { cwd: repository })
  let slow = false
  const requestedTools = new Set<string>()
  const server = createServer(async (request, response) => {
    let body = ''
    for await (const data of request) body += data
    const payload = JSON.parse(body) as { messages: { role: string; content: unknown }[] }
    response.writeHead(200, { 'content-type': 'text/event-stream' })
    const chunk = (delta: unknown, reason: string | null = null) => response.write(`data: ${JSON.stringify({ id: 'palette-test', object: 'chat.completion.chunk', created: 1, model: 'fixture', choices: [{ index: 0, delta, finish_reason: reason }] })}\n\n`)
    chunk({ role: 'assistant' })
    if (slow) { chunk({ content: '正在等待下一步' }); return }
    const tools = payload.messages.filter(message => message.role === 'tool')
    if (tools.length < 2) {
      const name = tools.length === 0 ? 'observe' : 'act'
      requestedTools.add(name)
      let parameters: Record<string, unknown> = {}
      if (tools.length) {
        const content = tools.at(-1)!.content
        const text = typeof content === 'string' ? content : (content as { type: string; text?: string }[]).map(part => part.text ?? '').join('')
        const frame = JSON.parse(text).frame as { frame_id: string; instance_id: string }
        parameters = { frame_id: frame.frame_id, instance_id: frame.instance_id, action: { type: 'click', x: 10, y: 10 } }
      }
      chunk({ tool_calls: [{ index: 0, id: `call-${tools.length}`, type: 'function', function: { name, arguments: JSON.stringify(parameters) } }] })
      chunk({}, 'tool_calls')
    } else {
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
    await page.getByRole('combobox', { name: '模型提供方', exact: true }).click()
    await page.getByRole('option', { name: '自定义端点', exact: true }).click()
    await page.getByLabel('Base URL', { exact: true }).fill(`http://127.0.0.1:${port}/v1`)
    await page.getByLabel('模型 ID', { exact: true }).fill('fixture')
    await page.getByLabel('API Key', { exact: true }).fill('e2e-secret')
    await page.getByRole('button', { name: '保存密钥', exact: true }).click()
    await expect(page.getByText('API Key 已配置', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '保存设置', exact: true }).click()
    await expect(page.getByText('设置已保存', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '主页', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Agent 验证', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Agent 验证', exact: true }).click()
    await page.getByRole('textbox', { name: '任务内容' }).fill('观察画面并单击一次')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await expect(page.getByText('已完成虚拟机视觉操作', { exact: true })).toBeVisible({ timeout: 30000 })
    await expect(page.getByRole('status').filter({ hasText: '已完成 · 2 次工具调用' })).toBeVisible()
    expect([...requestedTools].sort()).toEqual(['act', 'observe'])
    const reply = page.locator('[data-message-role=assistant] .agent-message-markdown').last()
    await expect(reply.getByRole('heading', { name: '已完成虚拟机视觉操作', level: 2 })).toBeVisible()
    await expect(reply.locator('strong')).toHaveText('操作结果')
    await expect(reply.getByRole('listitem')).toHaveCount(2)
    await expect(reply.locator('pre code.language-json')).toContainText('{"ok":true}')
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
    await page.screenshot({ path: 'test-results/pi-agent-workflow.png' })
    await page.evaluate(() => document.documentElement.classList.add('dark'))
    await page.screenshot({ path: 'test-results/pi-agent-markdown-dark.png', animations: 'disabled' })
    await page.evaluate(() => document.documentElement.classList.remove('dark'))
    slow = true
    await page.getByRole('textbox', { name: '任务内容' }).fill('等待任务')
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await expect(page.getByRole('button', { name: '停止 Agent', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '停止 Agent', exact: true }).click()
    await expect(page.getByRole('status').filter({ hasText: '已停止' })).toBeVisible({ timeout: 10000 })
    await page.evaluate(() => window.palette.finishClose('background'))
    await application.close()
    application = await launch()
    page = await application.firstWindow()
    await expect(page.getByText('后台已连接', { exact: true })).toBeVisible({ timeout: 30000 })
    await page.getByRole('button', { name: '观察画面并单击一次', exact: true }).click()
    await expect(page.getByText('已完成虚拟机视觉操作', { exact: true })).toBeVisible()
    await expect(page.getByRole('status').filter({ hasText: '已停止' })).toBeVisible()
    await page.evaluate(async id => { await window.palette.request('vms.stop', id); await window.palette.finishClose('shutdown') }, vmId)
  } finally {
    await application.evaluate(({ app }) => app.exit(0)).catch(() => {})
    await application.close().catch(() => {})
    server.closeAllConnections()
    await new Promise<void>(resolve => server.close(() => resolve()))
    try { execFileSync(uv, ['run', '--offline', 'python', '-m', 'palette.cli', '--root', root, 'daemon', 'stop'], { cwd: repository, stdio: 'ignore' }) } catch { /* It may have stopped through the UI. */ }
    await rm(root, { recursive: true, force: true })
  }
})
