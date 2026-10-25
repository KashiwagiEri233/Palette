import { BackendError, call } from './ipc'
import type { ShutdownTimeout } from '../shared/types'

type BackendCall = typeof call
interface ExitTarget { pid: number; generation: string }

export class VmShutdownTimeout extends Error {
  constructor(readonly target: ShutdownTimeout) { super('虚拟机未完成正常关机') }
}

export function processAlive(pid: number): boolean {
  try { process.kill(pid, 0); return true }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ESRCH') return false
    throw new Error('无法确认后台进程是否已退出，请检查后台状态')
  }
}

export class RuntimeShutdown {
  private pending = new Map<string, ExitTarget>()

  constructor(
    private backend: BackendCall = call,
    private alive: (pid: number) => boolean = processAlive,
    private now: () => number = Date.now,
    private sleep: (ms: number) => Promise<void> = ms => new Promise(resolve => setTimeout(resolve, ms))
  ) {}

  async stop(root: string): Promise<void> {
    let target = this.pending.get(root)
    if (!target) {
      const owner = await this.backend(root, 'hello')
      if (!Number.isSafeInteger(owner.pid) || Number(owner.pid) <= 0 || typeof owner.generation !== 'string' || !/^[a-f0-9]{32}$/.test(owner.generation)) throw new Error('后台进程身份无效，无法确认完全退出')
      const generation = owner.generation
      const supported = Array.isArray(owner.features) && owner.features.includes('runtime-stop')
      const identitySupported = Array.isArray(owner.features) && owner.features.includes('vm-stop-identity')
      if (!supported && owner.owner_kind !== 'daemon') throw new Error('当前 MCP 托管后台需要升级才能完全退出；可选择仅关闭窗口')
      const response = await this.backend(root, 'vms.list', undefined, {}, generation)
      const vms = response.vms as { vm_id: string; name?: string; status: { state: string; instance_id?: string; process_running?: boolean } }[]
      if (vms.some(vm => ['STARTING', 'STOPPING'].includes(vm.status.state))) throw new Error('虚拟机正在启动或关机，请等待操作完成后重试退出，或选择仅关闭窗口')
      const results = await Promise.allSettled(vms.filter(vm => !['STOPPED', 'FAILED'].includes(vm.status.state) || vm.status.process_running).map(async vm => {
        try {
          await this.backend(root, 'vms.stop', vm.vm_id, identitySupported && vm.status.instance_id ? { expected_instance_id: vm.status.instance_id } : {}, generation)
        } catch (error) {
          if (identitySupported && error instanceof BackendError && ['TIMEOUT', 'QMP_UNAVAILABLE', 'QMP_COMMAND_FAILED', 'ACTION_OUTCOME_UNKNOWN', 'GUEST_TOOL_UNAVAILABLE'].includes(error.code) && error.details?.operation === 'guest-shutdown' && error.details.vm_id === vm.vm_id && typeof error.details.instance_id === 'string' && error.details.instance_id === vm.status.instance_id) {
            throw new VmShutdownTimeout({ vm_id: vm.vm_id, name: vm.name ?? vm.vm_id, instance_id: error.details.instance_id })
          }
          throw error
        }
      }))
      const failed = results.find(result => result.status === 'rejected')
      if (failed?.status === 'rejected') throw failed.reason
      const current = await this.backend(root, 'hello', undefined, {}, generation)
      if (current.pid !== owner.pid || current.generation !== owner.generation) throw new Error('后台已更换，请检查状态后重试退出')
      target = { pid: Number(owner.pid), generation: owner.generation }
      // Keep the original identity even when the stop acknowledgement is lost.
      this.pending.set(root, target)
      try {
        const result = await this.backend(root, supported ? 'runtime.stop' : 'daemon.stop', undefined, {}, generation)
        if (result.stopping !== true) throw new Error('后台未确认停止，请检查状态后重试')
      } catch (error) {
        // A definite rejection permits a fresh attempt. An uncertain transport result
        // must only wait for this process, never replay a mutation or start an owner.
        if (/STATE_CONFLICT|INVALID_ARGUMENT|PRECONDITION_FAILED/.test(String(error))) this.pending.delete(root)
        throw error
      }
    }
    const deadline = this.now() + 15_000
    while (this.alive(target.pid)) {
      if (this.now() >= deadline) throw new Error('后台尚未退出，请检查状态；可重试等待或仅关闭窗口')
      await this.sleep(100)
    }
    this.pending.delete(root)
  }
}
