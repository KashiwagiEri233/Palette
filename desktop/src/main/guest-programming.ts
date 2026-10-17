export interface GuestProgrammingAdapter {
  readonly available: boolean
  execute(command: string): Promise<never>
  read(path: string): Promise<never>
  write(path: string, content: string): Promise<never>
  edit(path: string, oldText: string, newText: string): Promise<never>
}

export class UnavailableGuestProgramming implements GuestProgrammingAdapter {
  readonly available = false
  async execute(_command: string): Promise<never> { throw new Error('GUEST_TOOL_UNAVAILABLE: 客户机命令通道未接入') }
  async read(_path: string): Promise<never> { throw new Error('GUEST_TOOL_UNAVAILABLE: 客户机文件通道未接入') }
  async write(_path: string, _content: string): Promise<never> { throw new Error('GUEST_TOOL_UNAVAILABLE: 客户机文件通道未接入') }
  async edit(_path: string, _oldText: string, _newText: string): Promise<never> { throw new Error('GUEST_TOOL_UNAVAILABLE: 客户机文件通道未接入') }
}
