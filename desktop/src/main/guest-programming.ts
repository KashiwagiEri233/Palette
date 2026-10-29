export interface GuestProgrammingAdapter {
  readonly available: boolean
  execute(command: string): Promise<never>
  read(path: string): Promise<never>
  write(path: string, content: string): Promise<never>
  edit(path: string, oldText: string, newText: string): Promise<never>
}

export class UnavailableGuestProgramming implements GuestProgrammingAdapter {
  readonly available = false
  async execute(_command: string): Promise<never> { throw new Error('GUEST_TOOL_UNAVAILABLE: 请通过绑定虚拟机控制权的 PowerShell 增强工具执行命令') }
  async read(_path: string): Promise<never> { throw new Error('GUEST_TOOL_UNAVAILABLE: 请通过绑定虚拟机控制权的文件增强工具读取文件') }
  async write(_path: string, _content: string): Promise<never> { throw new Error('GUEST_TOOL_UNAVAILABLE: 请通过绑定虚拟机控制权的文件增强工具写入文件') }
  async edit(_path: string, _oldText: string, _newText: string): Promise<never> { throw new Error('GUEST_TOOL_UNAVAILABLE: 请通过绑定虚拟机控制权的文件增强工具编辑文件') }
}
