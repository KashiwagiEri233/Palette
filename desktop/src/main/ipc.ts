import { connect, type Socket } from 'node:net'
import { randomBytes } from 'node:crypto'
import { readFile, lstat } from 'node:fs/promises'

interface Endpoint { address: string; generation: string; token: string; transport: string; operation_timeout_seconds: number }

export class BackendError extends Error {
  constructor(readonly code: string, message: string, readonly details?: Record<string, unknown>) {
    super(`${code}: ${message}`)
  }
}

export async function readEndpoint(runtimeRoot: string): Promise<Endpoint> {
  const { join } = await import('node:path')
  const path = join(runtimeRoot, '.operator', 'endpoint.json')
  for (const candidate of [join(runtimeRoot, '.operator'), path]) {
    const stat = await lstat(candidate)
    if (stat.isSymbolicLink() || (process.platform !== 'win32' && ((stat.mode & 0o077) !== 0 || stat.uid !== process.getuid?.()))) throw new Error('后台连接文件权限不正确')
  }
  const data = await readFile(path, 'utf8')
  if (data.length > 4096) throw new Error('后台连接文件过大')
  const endpoint = JSON.parse(data) as Endpoint
  if (!/^[a-f0-9]{32}$/.test(endpoint.generation) || !/^[a-f0-9]{64}$/.test(endpoint.token)) throw new Error('后台连接文件无效')
  if (process.platform === 'win32' ? !endpoint.address.startsWith('\\\\.\\pipe\\palette-') : !endpoint.address.endsWith('.sock')) throw new Error('后台传输地址无效')
  return endpoint
}

export async function call(runtimeRoot: string, method: string, vmId?: string, payload: Record<string, unknown> = {}, expectedGeneration?: string): Promise<Record<string, unknown>> {
  const endpoint = await readEndpoint(runtimeRoot)
  if (expectedGeneration !== undefined && endpoint.generation !== expectedGeneration) throw new Error('后台已更换，请检查状态后重试退出')
  const requestId = randomBytes(16).toString('hex')
  const command = { method, vm_id: vmId ?? null, payload }
  const body = Buffer.from(JSON.stringify({ version: 1, generation: endpoint.generation, token: endpoint.token, request_id: requestId, command }))
  if (body.length > 256 * 1024) throw new Error('请求过大')
  return new Promise((resolve, reject) => {
    const socket = connect(endpoint.address)
    let received = Buffer.alloc(0)
    let expected: number | undefined
    let done = false
    const fail = (error: Error): void => { if (!done) { done = true; reject(error) }; socket.destroy() }
    socket.setTimeout(Math.min(3600_000, (endpoint.operation_timeout_seconds + 5) * 1000), () => fail(new Error('后台操作超时，请先检查虚拟机状态')))
    socket.on('connect', () => { const header = Buffer.alloc(4); header.writeUInt32BE(body.length); socket.write(Buffer.concat([header, body])) })
    socket.on('data', data => {
      received = Buffer.concat([received, data])
      if (expected === undefined && received.length >= 4) {
        expected = received.readUInt32BE(0)
        if (!expected || expected > 256 * 1024) { fail(new Error('后台响应过大')); return }
      }
      if (expected === undefined || received.length < expected + 4) return
      try {
        const response = JSON.parse(received.subarray(4, expected + 4).toString('utf8'))
        if (response.request_id !== requestId || response.generation !== endpoint.generation) throw new Error('后台响应身份不匹配')
        if (!response.ok) throw new BackendError(response.error?.code ?? 'ERROR', response.error?.message ?? '操作失败', response.error?.details)
        done = true; resolve(response.result); socket.end()
      } catch (error) { fail(error as Error) }
    })
    socket.on('error', fail)
    socket.on('close', () => { if (!done) fail(new Error('后台连接中断，操作结果未知；请先检查状态')) })
  })
}

/** Main-only, lease-bound transport. Never retries a frame that may have been written. */
export class InputStream {
  private readonly socket: Socket
  private readonly ready: Promise<void>
  private received = Buffer.alloc(0)
  private closed = false
  private terminalError = new Error('输入通道已关闭，请重新接管')
  private failure?: Error
  private failureHandler?: (error: Error) => void
  private pending?: {
    id: string
    method: string
    resolve: (result: Record<string, unknown>) => void
    reject: (error: Error) => void
    timer: ReturnType<typeof setTimeout>
  }

  private constructor(private endpoint: Endpoint, readonly vmId: string, readonly leaseId: string) {
    this.socket = connect(endpoint.address)
    this.ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail(new Error('输入通道连接超时'), 'connect_timeout'), 2000)
      this.socket.once('connect', () => { clearTimeout(timer); resolve() })
      this.socket.once('error', error => { clearTimeout(timer); reject(error) })
      this.socket.once('close', () => { clearTimeout(timer); reject(this.terminalError) })
    })
    this.socket.on('data', data => this.receive(data))
    this.socket.on('error', error => this.fail(error, 'socket_error'))
    this.socket.on('end', () => this.fail(new Error('后台关闭了输入连接'), 'peer_end'))
    this.socket.on('close', () => this.fail(new Error('后台输入连接已断开'), 'socket_close'))
  }

  static async open(runtimeRoot: string, vmId: string, leaseId: string, generation: string): Promise<InputStream> {
    const endpoint = await readEndpoint(runtimeRoot)
    if (endpoint.generation !== generation) throw new Error('后台已更换，请重新接管')
    const stream = new InputStream(endpoint, vmId, leaseId)
    try {
      const result = await stream.request('control.stream.open', { lease_id: leaseId })
      if (result.stream !== 'operator-input-stream-v1') throw new Error('输入通道协议不兼容')
      return stream
    } catch (error) { stream.close(); throw error }
  }

  input(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (payload.lease_id !== this.leaseId) return Promise.reject(new Error('人工控制租约已更换'))
    return this.request('control.input', payload)
  }

  setFailureHandler(handler: (error: Error) => void): void {
    this.failureHandler = handler
    if (this.failure) handler(this.failure)
  }

  private fail(error: Error, phase: string): void {
    if (this.closed) return
    const uncertain = this.pending?.method === 'control.input'
    this.failure = error instanceof BackendError ? error : new BackendError(
      uncertain ? 'ACTION_OUTCOME_UNKNOWN' : 'INPUT_CHANNEL_CLOSED',
      uncertain ? '人工输入连接中断，本批输入结果未知；请检查画面后重新接管' : '人工输入连接已断开，请重新接管',
      { phase, effect_state: uncertain ? 'unknown' : 'not_submitted', transport_error: { name: error.name, code: (error as NodeJS.ErrnoException).code, message: error.message, stack: error.stack } }
    )
    this.close(this.failure)
    this.failureHandler?.(this.failure)
  }

  close(error = new Error('输入通道已关闭')): void {
    if (this.closed) return
    this.closed = true
    this.terminalError = error
    const pending = this.pending
    this.pending = undefined
    if (pending) { clearTimeout(pending.timer); pending.reject(error) }
    this.socket.destroy()
  }

  private async request(method: string, payload: Record<string, unknown>): Promise<Record<string, unknown>> {
    await this.ready
    if (this.closed) throw this.terminalError
    if (this.pending) throw new Error('输入批次仍在处理中')
    const id = randomBytes(16).toString('hex')
    const body = Buffer.from(JSON.stringify({ version: 1, generation: this.endpoint.generation, token: this.endpoint.token, request_id: id, command: { method, vm_id: this.vmId, payload } }))
    if (body.length > 256 * 1024) throw new Error('请求过大')
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail(new Error('输入响应超时'), 'response_timeout'), 15000)
      this.pending = { id, method, resolve, reject, timer }
      const header = Buffer.alloc(4); header.writeUInt32BE(body.length)
      try {
        this.socket.write(Buffer.concat([header, body]), error => { if (error) this.fail(error, 'write') })
      } catch (error) { this.fail(error as Error, 'write') }
    })
  }

  private receive(data: Buffer): void {
    if (this.closed) return
    this.received = Buffer.concat([this.received, data])
    try {
      while (this.received.length >= 4) {
        const size = this.received.readUInt32BE(0)
        if (!size || size > 256 * 1024) throw new Error('后台响应过大')
        if (this.received.length < size + 4) return
        const response = JSON.parse(this.received.subarray(4, size + 4).toString('utf8'))
        this.received = this.received.subarray(size + 4)
        const pending = this.pending
        if (!pending || response.version !== 1 || response.request_id !== pending.id || response.generation !== this.endpoint.generation) throw new Error('后台响应身份不匹配')
        if (!response.ok) throw new BackendError(response.error?.code ?? 'ERROR', response.error?.message ?? '操作失败', response.error?.details)
        if (!response.result || typeof response.result !== 'object' || Array.isArray(response.result)) throw new Error('后台响应无效')
        clearTimeout(pending.timer); this.pending = undefined
        pending.resolve(response.result)
      }
    } catch (error) { this.fail(error as Error, 'response') }
  }
}
