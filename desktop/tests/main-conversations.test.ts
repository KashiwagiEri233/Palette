import { afterAll, beforeAll, beforeEach, expect, it, vi } from 'vitest'

type RequestHandler = (event: { sender: unknown; senderFrame: { url: string } }, method: string, vmId?: string, payload?: Record<string, unknown>) => Promise<unknown>

const fixture = vi.hoisted(() => ({
  handlers: new Map<string, RequestHandler>(),
  contents: {
    session: { setPermissionRequestHandler: vi.fn(), webRequest: { onHeadersReceived: vi.fn() } },
    setWindowOpenHandler: vi.fn(), on: vi.fn(), send: vi.fn()
  },
  service: { settings: vi.fn(), snapshot: vi.fn(), stop: vi.fn(), stopVm: vi.fn(), remove: vi.fn(), removeVm: vi.fn(), diagnostics: vi.fn(), redactDiagnostics: vi.fn() },
  repository: { list: vi.fn(), update: vi.fn(), remove: vi.fn(), removeVm: vi.fn() },
  backend: vi.fn(), clipboard: vi.fn(), openStream: vi.fn()
}))

vi.mock('electron', () => ({
  app: { whenReady: () => Promise.resolve(), getPath: () => 'unused', getVersion: () => 'test-version', on: vi.fn() },
  clipboard: { writeText: fixture.clipboard },
  BrowserWindow: vi.fn(function () {
    return { webContents: fixture.contents, removeMenu: vi.fn(), on: vi.fn(), loadURL: vi.fn(), isFocused: () => true, isDestroyed: () => false }
  }),
  ipcMain: { handle: (channel: string, handler: RequestHandler) => fixture.handlers.set(channel, handler) },
  protocol: { registerSchemesAsPrivileged: vi.fn(), handle: vi.fn() },
  dialog: {}, net: {}, Menu: {}, safeStorage: {}, shell: {}
}))
vi.mock('../src/main/ipc', () => ({ call: fixture.backend, InputStream: { open: fixture.openStream } }))
vi.mock('node:child_process', async () => {
  const { EventEmitter } = await import('node:events')
  return { spawn: vi.fn(() => {
    const child = Object.assign(new EventEmitter(), { stdout: new EventEmitter(), stderr: new EventEmitter(), kill: vi.fn() })
    queueMicrotask(() => { child.stdout.emit('data', JSON.stringify({ runtime_root: 'test-runtime' })); child.emit('exit', 0) })
    return child
  }) }
})
vi.mock('../src/main/conversations', () => ({ ConversationRepository: vi.fn(function () { return fixture.repository }) }))
vi.mock('../src/main/pi-agent', () => ({ PiAgentService: vi.fn(function () { return fixture.service }) }))

function request(method: string, vmId?: string, payload: Record<string, unknown> = {}) {
  return fixture.handlers.get('request')!({ sender: fixture.contents, senderFrame: { url: 'app://palette/index.html' } }, method, vmId, payload)
}

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>(done => { resolve = done })
  return { promise, resolve }
}

beforeAll(async () => {
  vi.stubEnv('PALETTE_ROOT', process.cwd())
  vi.stubEnv('ELECTRON_RENDERER_URL', '')
  await import('../src/main/index')
  await vi.waitFor(() => expect(fixture.handlers.has('request')).toBe(true))
})
afterAll(() => vi.unstubAllEnvs())
beforeEach(async () => {
  vi.clearAllMocks()
  for (const mock of [...Object.values(fixture.service), ...Object.values(fixture.repository), fixture.backend]) mock.mockReset()
  fixture.backend.mockResolvedValue({ vms: [{ vm_id: 'vm' }], lease_id: 'lease' })
  fixture.service.stop.mockResolvedValue({})
  fixture.service.stopVm.mockResolvedValue(undefined)
  fixture.service.diagnostics.mockResolvedValue({ model: { modelId: 'fixture' } })
  fixture.service.redactDiagnostics.mockImplementation(async value => value.replaceAll('saved-secret', '[redacted]'))
  await request('agent.settings.get')
})

function copyDebugInfo(request: unknown, sender: unknown = fixture.contents) {
  const handler = fixture.handlers.get('copy-debug-info')! as unknown as (event: unknown, request: unknown) => Promise<unknown>
  return handler({ sender, senderFrame: { url: 'app://palette/index.html' } }, request)
}

it('copies main-process failure evidence with app metadata and redacted credentials', async () => {
  const error = Object.assign(new Error('copy-test QMP unavailable saved-secret'), { code: 'STARTUP_FAILED', details: { stderr: ['-vnc: Failed to bind socket'] } })
  fixture.backend.mockRejectedValueOnce(error)
  await expect(request('vms.start', 'vm')).rejects.toThrow('copy-test')
  await expect(copyDebugInfo({ source: 'application', error: error.message, vmId: 'vm' })).resolves.toEqual({ copied: true })
  const report = JSON.parse(fixture.clipboard.mock.calls[0][0])
  expect(report).toMatchObject({ format: 'palette-debug-v1', application: { version: 'test-version' }, recentFailure: { operation: 'vms.start', error: { code: 'STARTUP_FAILED', details: { stderr: ['-vnc: Failed to bind socket'] } } } })
  expect(fixture.clipboard.mock.calls[0][0]).not.toContain('saved-secret')
})

it('uses the requested conversation diagnostics and reports clipboard errors', async () => {
  fixture.service.diagnostics.mockResolvedValueOnce({ modelError: 'HTTP 400 invalid schema', run: { state: 'failed' } })
  fixture.clipboard.mockImplementationOnce(() => { throw new Error('clipboard unavailable') })
  const request = { source: 'agent', error: 'tool schema failed', vmId: 'vm', conversationId: 'conversation' }
  await expect(copyDebugInfo(request)).rejects.toThrow('clipboard unavailable')
  expect(fixture.service.diagnostics).toHaveBeenCalledWith('vm', 'conversation')
  await expect(copyDebugInfo(request)).resolves.toEqual({ copied: true })
})

it('rejects untrusted senders, oversized errors and invalid conversation identifiers', async () => {
  for (const request of [{ source: 'agent', error: 'failed' }, { source: 'application', error: 'x'.repeat(16001) }, { source: 'agent', error: 'failed', vmId: 'vm', conversationId: '../other' }]) await expect(copyDebugInfo(request)).rejects.toThrow()
  await expect(copyDebugInfo({ source: 'application', error: 'failed' }, {})).rejects.toThrow()
  expect(fixture.clipboard).not.toHaveBeenCalled()
})

it('waits for only the archived session to stop before persisting the update', async () => {
  const { promise, resolve } = deferred()
  fixture.service.stop.mockReturnValueOnce(promise)
  const patch = { archived: true }
  const pending = request('conversations.update', 'vm', { id: 'old', revision: 1, patch })
  await vi.waitFor(() => expect(fixture.service.stop).toHaveBeenCalledWith('vm', 'old'))
  expect(fixture.service.stopVm).not.toHaveBeenCalled()
  expect(fixture.repository.update).not.toHaveBeenCalled()
  resolve()
  await pending
  expect(fixture.repository.update).toHaveBeenCalledWith('vm', 'old', 1, patch)
})

it.each([{ archived: false }, { title: 'Renamed' }, { draft: 'Saved draft' }])('does not stop tasks for an ordinary update %j', async patch => {
  await request('conversations.update', 'vm', { id: 'old', revision: 1, patch })
  expect(fixture.service.stop).not.toHaveBeenCalled()
  expect(fixture.service.stopVm).not.toHaveBeenCalled()
  expect(fixture.repository.update).toHaveBeenCalledWith('vm', 'old', 1, patch)
})

it('does not archive a session if stopping it fails', async () => {
  fixture.service.stop.mockRejectedValueOnce(new Error('stop failed'))
  await expect(request('conversations.update', 'vm', { id: 'old', revision: 1, patch: { archived: true } })).rejects.toThrow('stop failed')
  expect(fixture.repository.update).not.toHaveBeenCalled()
})

it('deletes only the target and cleans agent files after the metadata is removed', async () => {
  const { promise, resolve } = deferred()
  fixture.service.stop.mockReturnValueOnce(promise)
  const pending = request('conversations.delete', 'vm', { id: 'old', revision: 1 })
  await vi.waitFor(() => expect(fixture.service.stop).toHaveBeenCalledWith('vm', 'old'))
  expect(fixture.repository.remove).not.toHaveBeenCalled()
  expect(fixture.service.remove).not.toHaveBeenCalled()
  resolve()
  await expect(pending).resolves.toEqual({ deleted: true })
  expect(fixture.service.snapshot).toHaveBeenCalledWith('vm', 'old')
  expect(fixture.repository.remove).toHaveBeenCalledWith('vm', 'old', 1)
  expect(fixture.service.remove).toHaveBeenCalledWith('vm', 'old')
  expect(fixture.service.stopVm).not.toHaveBeenCalled()
  expect(fixture.service.snapshot.mock.invocationCallOrder[0]).toBeLessThan(fixture.service.stop.mock.invocationCallOrder[0])
  expect(fixture.service.stop.mock.invocationCallOrder[0]).toBeLessThan(fixture.repository.remove.mock.invocationCallOrder[0])
  expect(fixture.repository.remove.mock.invocationCallOrder[0]).toBeLessThan(fixture.service.remove.mock.invocationCallOrder[0])
})

it('does not clean agent files when deleting metadata fails', async () => {
  fixture.repository.remove.mockRejectedValueOnce(new Error('revision conflict'))
  await expect(request('conversations.delete', 'vm', { id: 'old', revision: 1 })).rejects.toThrow('revision conflict')
  expect(fixture.service.remove).not.toHaveBeenCalled()
  expect(fixture.service.stopVm).not.toHaveBeenCalled()
})

it.each(['control.acquire', 'vms.stop', 'vms.restart', 'vms.reset', 'vms.delete'])('still stops the whole VM before %s', async method => {
  const { promise, resolve } = deferred()
  fixture.service.stopVm.mockReturnValueOnce(promise)
  const pending = request(method, 'vm')
  await vi.waitFor(() => expect(fixture.service.stopVm).toHaveBeenCalledWith('vm'))
  expect(fixture.backend).not.toHaveBeenCalled()
  expect(fixture.service.stop).not.toHaveBeenCalled()
  resolve()
  await pending
  expect(fixture.backend).toHaveBeenCalledWith(expect.any(String), method, 'vm', {}, undefined)
})

it('rejects hardware reboot on an older backend without stopping agents or falling back', async () => {
  fixture.backend.mockResolvedValue({ features: [], generation: 'a'.repeat(32) })
  await expect(request('vms.reboot', 'vm', { expected_instance_id: 'instance' })).rejects.toThrow('后台需要升级')
  expect(fixture.service.stopVm).not.toHaveBeenCalled()
  expect(fixture.backend.mock.calls.map(([, method]) => method)).toEqual(['hello'])
})

it('requires a target instance before hardware reboot', async () => {
  await expect(request('vms.reboot', 'vm')).rejects.toThrow('重启目标实例无效')
  expect(fixture.backend).not.toHaveBeenCalled()
  expect(fixture.service.stopVm).not.toHaveBeenCalled()
})

it('stops agents and closes the human stream before rebooting the expected backend generation', async () => {
  const generation = 'b'.repeat(32)
  fixture.backend.mockImplementation(async (_root, method) => {
    if (method === 'hello') return { desktop_protocol: 1, features: ['operator-input-stream-v1', 'vm-reboot-v1'], generation }
    if (method === 'control.acquire') return { lease_id: 'reboot-lease' }
    return {}
  })
  await fixture.handlers.get('connect')!({ sender: fixture.contents, senderFrame: { url: 'app://palette/index.html' } }, '')
  const stream = { leaseId: 'reboot-lease', setFailureHandler: vi.fn(), close: vi.fn(), input: vi.fn() }
  fixture.openStream.mockResolvedValueOnce(stream)
  await request('control.acquire', 'vm')
  fixture.service.stopVm.mockClear()
  const stopping = deferred()
  fixture.service.stopVm.mockReturnValueOnce(stopping.promise)
  const payload = { expected_instance_id: 'instance' }
  const pending = request('vms.reboot', 'vm', payload)
  await vi.waitFor(() => expect(fixture.service.stopVm).toHaveBeenCalledWith('vm'))
  expect(stream.close).not.toHaveBeenCalled()
  expect(fixture.backend.mock.calls.some(([, method]) => method === 'vms.reboot')).toBe(false)
  stopping.resolve()
  await pending
  expect(stream.close).toHaveBeenCalledOnce()
  expect(fixture.contents.send).toHaveBeenCalledWith('control-lost', { vmId: 'vm', leaseId: 'reboot-lease' })
  expect(fixture.backend).toHaveBeenCalledWith('test-runtime', 'vms.reboot', 'vm', payload, generation)
  await expect(request('control.input', 'vm', { lease_id: 'reboot-lease', seq: 1 })).rejects.toThrow('请重新接管')
  const notifications = fixture.contents.send.mock.calls.length
  stream.setFailureHandler.mock.calls[0][0](new Error('late failure'))
  expect(fixture.contents.send).toHaveBeenCalledTimes(notifications)
  expect(fixture.backend.mock.calls.some(([, method]) => method === 'vms.restart' || method === 'vms.stop')).toBe(false)
})

it('releases a failed input stream without falling back or allowing its late callback to release a newer lease', async () => {
  let acquisitions = 0
  fixture.backend.mockImplementation(async (_root, method) => {
    if (method === 'hello') return { desktop_protocol: 1, generation: 'a'.repeat(32), features: ['operator-input-stream-v1'] }
    if (method === 'control.acquire') return { lease_id: `lease-${++acquisitions}` }
    if (method === 'control.release') return { release_confirmed: true }
    return {}
  })
  await fixture.handlers.get('connect')!({ sender: fixture.contents, senderFrame: { url: 'app://palette/index.html' } }, '')
  const error = new Error('人工输入连接中断，本批输入结果未知；请检查画面后重新接管')
  const first = { leaseId: 'lease-1', setFailureHandler: vi.fn(), close: vi.fn(), input: vi.fn().mockRejectedValue(error) }
  const second = { leaseId: 'lease-2', setFailureHandler: vi.fn(), close: vi.fn(), input: vi.fn() }
  fixture.openStream.mockResolvedValueOnce(first).mockResolvedValueOnce(second)
  await request('control.acquire', 'vm')
  first.setFailureHandler.mock.calls[0][0](error)
  expect(fixture.contents.send).toHaveBeenCalledWith('control-lost', { vmId: 'vm', leaseId: 'lease-1', error: error.message })
  await vi.waitFor(() => expect(fixture.backend).toHaveBeenCalledWith('test-runtime', 'control.release', 'vm', { lease_id: 'lease-1' }, 'a'.repeat(32)))
  await expect(request('control.input', 'vm', { lease_id: 'lease-1', seq: 1 })).rejects.toBe(error)
  expect(fixture.backend.mock.calls.some(([, method]) => method === 'control.input')).toBe(false)
  await request('control.acquire', 'vm')
  const calls = fixture.backend.mock.calls.length
  const events = fixture.contents.send.mock.calls.length
  first.setFailureHandler.mock.calls[0][0](error)
  expect(fixture.backend).toHaveBeenCalledTimes(calls)
  expect(fixture.contents.send).toHaveBeenCalledTimes(events)
  await request('control.release', 'vm', { lease_id: 'lease-2' })
  await expect(request('control.input', 'vm', { lease_id: 'lease-2', seq: 2 })).rejects.toThrow('请重新接管')
  expect(fixture.backend.mock.calls.some(([, method]) => method === 'control.input')).toBe(false)
})
