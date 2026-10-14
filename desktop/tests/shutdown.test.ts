import { describe, it, expect, vi } from 'vitest'
import { RuntimeShutdown, processAlive } from '../src/main/shutdown'

const owner = { pid: 12345, generation: 'a'.repeat(32), owner_kind: 'daemon', features: ['runtime-stop'] }

function fixture(options: { owner?: Record<string, unknown>; vms?: { vm_id: string; status: { state: string } }[]; alive?: () => boolean } = {}) {
  const backend = vi.fn(async (_root: string, method: string): Promise<Record<string, unknown>> => {
    if (method === 'hello') return options.owner ?? owner
    if (method === 'vms.list') return { vms: options.vms ?? [] }
    return { stopping: true }
  })
  let time = 0
  const sleep = vi.fn(async (ms: number) => { time += ms })
  const shutdown = new RuntimeShutdown(backend, options.alive ?? (() => false), () => time, sleep)
  return { backend, sleep, shutdown }
}

describe('complete runtime exit', () => {
  it('stops an empty backend and waits for its process', async () => {
    let checks = 0
    const { shutdown, backend, sleep } = fixture({ alive: () => checks++ < 2 })
    await shutdown.stop('root')
    expect(backend.mock.calls.map(call => call[1])).toEqual(['hello', 'vms.list', 'hello', 'runtime.stop'])
    expect(backend).toHaveBeenLastCalledWith('root', 'runtime.stop', undefined, {}, owner.generation)
    expect(sleep).toHaveBeenCalledTimes(2)
  })

  it('gracefully stops active machines and never stops the backend after a shutdown failure', async () => {
    const { shutdown, backend } = fixture({ vms: [{ vm_id: 'running', status: { state: 'RUNNING' } }, { vm_id: 'stopped', status: { state: 'STOPPED' } }] })
    backend.mockImplementation(async (_root, method) => {
      if (method === 'hello') return owner
      if (method === 'vms.list') return { vms: [{ vm_id: 'running', status: { state: 'RUNNING' } }] }
      if (method === 'vms.stop') throw new Error('ACPI timeout')
      return { stopping: true }
    })
    await expect(shutdown.stop('root')).rejects.toThrow('ACPI timeout')
    expect(backend.mock.calls.map(call => call[1])).toEqual(['hello', 'vms.list', 'vms.stop'])
    backend.mockResolvedValueOnce(owner).mockResolvedValueOnce({ vms: [] }).mockResolvedValueOnce(owner).mockResolvedValueOnce({ stopping: true })
    await shutdown.stop('root')
    expect(backend.mock.calls.at(-1)?.[1]).toBe('runtime.stop')
  })

  it('stops running and paused machines in order while leaving stopped records alone', async () => {
    const { shutdown, backend } = fixture({ vms: [{ vm_id: 'running', status: { state: 'RUNNING' } }, { vm_id: 'stopped', status: { state: 'STOPPED' } }, { vm_id: 'paused', status: { state: 'PAUSED' } }] })
    await shutdown.stop('root')
    expect(backend.mock.calls.map(call => call[1])).toEqual(['hello', 'vms.list', 'vms.stop', 'vms.stop', 'hello', 'runtime.stop'])
  })

  it('keeps daemon compatibility and refuses an older embedded backend before stopping VMs', async () => {
    const daemon = fixture({ owner: { ...owner, features: [] } })
    await daemon.shutdown.stop('root')
    expect(daemon.backend.mock.calls.at(-1)?.[1]).toBe('daemon.stop')
    const embedded = fixture({ owner: { ...owner, owner_kind: 'embedded', features: [] } })
    await expect(embedded.shutdown.stop('root')).rejects.toThrow('需要升级')
    expect(embedded.backend).toHaveBeenCalledTimes(1)
  })

  it('refuses invalid identities and a replacement owner', async () => {
    const invalid = fixture({ owner: { ...owner, pid: undefined } })
    await expect(invalid.shutdown.stop('root')).rejects.toThrow('身份无效')
    const replacement = fixture()
    replacement.backend.mockResolvedValueOnce(owner).mockResolvedValueOnce({ vms: [] }).mockResolvedValueOnce({ ...owner, generation: 'b'.repeat(32) })
    await expect(replacement.shutdown.stop('root')).rejects.toThrow('后台已更换')
    expect(replacement.backend.mock.calls.some(call => call[1] === 'runtime.stop')).toBe(false)
  })

  it('retries a definite concurrent-start rejection', async () => {
    const { shutdown, backend } = fixture()
    backend.mockResolvedValueOnce(owner).mockResolvedValueOnce({ vms: [] }).mockResolvedValueOnce(owner).mockRejectedValueOnce(new Error('STATE_CONFLICT: still running'))
    await expect(shutdown.stop('root')).rejects.toThrow('STATE_CONFLICT')
    await shutdown.stop('root')
    expect(backend.mock.calls.filter(call => call[1] === 'runtime.stop')).toHaveLength(2)
  })

  it('retries only waiting after timeout or an uncertain stop acknowledgement', async () => {
    let alive = true
    const { shutdown, backend } = fixture({ alive: () => alive })
    await expect(shutdown.stop('root')).rejects.toThrow('后台尚未退出')
    const calls = backend.mock.calls.length
    alive = false
    await shutdown.stop('root')
    expect(backend).toHaveBeenCalledTimes(calls)
    alive = true
    backend.mockResolvedValueOnce(owner).mockResolvedValueOnce({ vms: [] }).mockResolvedValueOnce(owner).mockRejectedValueOnce(new Error('connection result unknown'))
    await expect(shutdown.stop('root')).rejects.toThrow('unknown')
    const uncertainCalls = backend.mock.calls.length
    alive = false
    await shutdown.stop('root')
    expect(backend).toHaveBeenCalledTimes(uncertainCalls)
  })

  it('distinguishes a dead process from an unconfirmed process check', () => {
    const kill = vi.spyOn(process, 'kill')
    try {
      kill.mockImplementation(() => { throw Object.assign(new Error(), { code: 'ESRCH' }) })
      expect(processAlive(12345)).toBe(false)
      kill.mockImplementation(() => { throw Object.assign(new Error(), { code: 'EPERM' }) })
      expect(() => processAlive(12345)).toThrow('无法确认')
    } finally { kill.mockRestore() }
  })
})
