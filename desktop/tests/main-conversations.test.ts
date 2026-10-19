import { afterAll, beforeAll, beforeEach, expect, it, vi } from 'vitest'

type RequestHandler = (event: { sender: unknown; senderFrame: { url: string } }, method: string, vmId?: string, payload?: Record<string, unknown>) => Promise<unknown>

const fixture = vi.hoisted(() => ({
  handlers: new Map<string, RequestHandler>(),
  contents: {
    session: { setPermissionRequestHandler: vi.fn(), webRequest: { onHeadersReceived: vi.fn() } },
    setWindowOpenHandler: vi.fn(), on: vi.fn(), send: vi.fn()
  },
  service: { settings: vi.fn(), snapshot: vi.fn(), stop: vi.fn(), stopVm: vi.fn(), remove: vi.fn(), removeVm: vi.fn() },
  repository: { list: vi.fn(), update: vi.fn(), remove: vi.fn(), removeVm: vi.fn() },
  backend: vi.fn()
}))

vi.mock('electron', () => ({
  app: { whenReady: () => Promise.resolve(), getPath: () => 'unused', on: vi.fn() },
  BrowserWindow: vi.fn(function () {
    return { webContents: fixture.contents, removeMenu: vi.fn(), on: vi.fn(), loadURL: vi.fn(), isFocused: () => true, isDestroyed: () => false }
  }),
  ipcMain: { handle: (channel: string, handler: RequestHandler) => fixture.handlers.set(channel, handler) },
  protocol: { registerSchemesAsPrivileged: vi.fn(), handle: vi.fn() },
  dialog: {}, net: {}, Menu: {}, safeStorage: {}, shell: {}
}))
vi.mock('../src/main/ipc', () => ({ call: fixture.backend }))
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
  await request('agent.settings.get')
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
