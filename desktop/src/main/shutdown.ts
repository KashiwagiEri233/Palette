import { call } from './ipc'

type BackendCall = typeof call
interface ExitTarget { pid: number; generation: string }

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
      const supported = Array.isArray(owner.features) && owner.features.includes('runtime-stop')
      if (!supported && owner.owner_kind !== 'daemon') throw new Error('当前 MCP 托管后台需要升级才能完全退出；可选择仅关闭窗口')
      const response = await this.backend(root, 'vms.list', undefined, {}, owner.generation)
      for (const vm of response.vms as { vm_id: string; status: { state: string } }[]) {
        if (!['STOPPED', 'FAILED'].includes(vm.status.state)) await this.backend(root, 'vms.stop', vm.vm_id, {}, owner.generation)
      }
      const current = await this.backend(root, 'hello', undefined, {}, owner.generation)
      if (current.pid !== owner.pid || current.generation !== owner.generation) throw new Error('后台已更换，请检查状态后重试退出')
      target = { pid: Number(owner.pid), generation: owner.generation }
      // Keep the original identity even when the stop acknowledgement is lost.
      this.pending.set(root, target)
      try {
        const result = await this.backend(root, supported ? 'runtime.stop' : 'daemon.stop', undefined, {}, owner.generation)
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
