import { connect, type Socket } from 'node:net'
import { randomBytes } from 'node:crypto'
import { readFile, lstat } from 'node:fs/promises'
import { join } from 'node:path'

interface Endpoint { address: string; generation: string; token: string; transport: string; operation_timeout_seconds: number }

export class BackendError extends Error {
  constructor(readonly code: string, message: string, readonly details?: Record<string, unknown>) {
    super(`${code}: ${message}`)
  }
}

export async function readEndpoint(runtimeRoot: string): Promise<Endpoint> {
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

const endpointCache = new Map<string, Endpoint>()

class RetryableConnectError extends Error {
  constructor(readonly causeError: Error) {
    super(causeError.message)
  }
}

function callOnce(endpoint: Endpoint, method: string, vmId: string | undefined, payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  const requestId = randomBytes(16).toString('hex')
  const command = { method, vm_id: vmId ?? null, payload }
  const body = Buffer.from(JSON.stringify({ version: 1, generation: endpoint.generation, token: endpoint.token, request_id: requestId, command }))
  if (body.length > 256 * 1024) throw new Error('请求过大')
  return new Promise((resolve, reject) => {
    const socket = connect(endpoint.address)
    let received = Buffer.alloc(0)
    let expected: number | undefined
    let connected = false
    let done = false
    const fail = (error: Error): void => {
      if (done) return
      done = true
      const code = (error as NodeJS.ErrnoException).code
      if (!connected && (code === 'ENOENT' || code === 'ECONNREFUSED')) reject(new RetryableConnectError(error))
      else reject(error)
      socket.destroy()
    }
    socket.setTimeout(Math.min(3600_000, (endpoint.operation_timeout_seconds + 5) * 1000), () => fail(new Error('后台操作超时，请先检查虚拟机状态')))
    socket.on('connect', () => { connected = true; const header = Buffer.alloc(4); header.writeUInt32BE(body.length); socket.write(Buffer.concat([header, body])) })
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

export async function call(runtimeRoot: string, method: string, vmId?: string, payload: Record<string, unknown> = {}, expectedGeneration?: string): Promise<Record<string, unknown>> {
  let endpoint = expectedGeneration === undefined ? endpointCache.get(runtimeRoot) : undefined
  if (!endpoint) {
    endpoint = await readEndpoint(runtimeRoot)
    endpointCache.set(runtimeRoot, endpoint)
  }
  if (expectedGeneration !== undefined && endpoint.generation !== expectedGeneration) throw new Error('后台已更换，请检查状态后重试退出')
  try {
    return await callOnce(endpoint, method, vmId, payload)
  } catch (error) {
    if (error instanceof RetryableConnectError) {
      endpointCache.delete(runtimeRoot)
      if (expectedGeneration === undefined) {
        const fresh = await readEndpoint(runtimeRoot)
        endpointCache.set(runtimeRoot, fresh)
        try {
          return await callOnce(fresh, method, vmId, payload)
        } catch (retryError) {
          throw retryError instanceof RetryableConnectError ? retryError.causeError : retryError
        }
      }
      throw error.causeError
    }
    throw error
  }
}

interface PendingRequest {
  id: string
  method: string
  resolve: (result: Record<string, unknown>) => void
  reject: (error: Error) => void
  timer: ReturnType<typeof setTimeout>
}

const MAX_IN_FLIGHT = 4

/** Main-only, lease-bound transport. Never retries a frame that may have been written. */
export class InputStream {
  private readonly socket: Socket
  private readonly ready: Promise<void>
  private readyResolved = false
  private received: Buffer<ArrayBufferLike> = Buffer.alloc(0)
  private closed = false
  private terminalError = new Error('输入通道已关闭，请重新接管')
  private failure?: Error
  private failureHandler?: (error: Error) => void
  private pending = new Map<string, PendingRequest>()

  private constructor(private endpoint: Endpoint, readonly vmId: string, readonly leaseId: string) {
    this.socket = connect(endpoint.address)
    this.ready = new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail(new Error('输入通道连接超时'), 'connect_timeout'), 2000)
      this.socket.once('connect', () => { clearTimeout(timer); this.readyResolved = true; resolve() })
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
    let uncertain = false
    for (const item of this.pending.values()) {
      if (item.method === 'control.input') { uncertain = true; break }
    }
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
    const items = [...this.pending.values()]
    this.pending.clear()
    for (const item of items) {
      clearTimeout(item.timer)
      item.reject(error)
    }
    this.socket.destroy()
  }

  private async request(method: string, payload: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (!this.readyResolved) await this.ready
    if (this.closed) throw this.terminalError
    if (this.pending.size >= MAX_IN_FLIGHT) throw new Error('输入队列已满')
    const id = randomBytes(16).toString('hex')
    const body = Buffer.from(JSON.stringify({ version: 1, generation: this.endpoint.generation, token: this.endpoint.token, request_id: id, command: { method, vm_id: this.vmId, payload } }))
    if (method === 'control.input' && body.length > 64 * 1024 + 512) throw new Error('请求无效')
    if (body.length > 256 * 1024) throw new Error('请求过大')
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail(new Error('输入响应超时'), 'response_timeout'), 15000)
      this.pending.set(id, { id, method, resolve, reject, timer })
      const header = Buffer.alloc(4); header.writeUInt32BE(body.length)
      try {
        this.socket.write(Buffer.concat([header, body]), error => { if (error) this.fail(error, 'write') })
      } catch (error) { this.fail(error as Error, 'write') }
    })
  }

  private receive(data: Buffer<ArrayBufferLike>): void {
    if (this.closed) return
    this.received = this.received.length ? Buffer.concat([this.received, data]) : data
    try {
      while (this.received.length >= 4) {
        const size = this.received.readUInt32BE(0)
        if (!size || size > 256 * 1024) throw new Error('后台响应过大')
        if (this.received.length < size + 4) return
        const response = JSON.parse(this.received.subarray(4, size + 4).toString('utf8'))
        this.received = this.received.subarray(size + 4)
        const pending = typeof response.request_id === 'string' ? this.pending.get(response.request_id) : undefined
        if (!pending || response.version !== 1 || response.generation !== this.endpoint.generation) throw new Error('后台响应身份不匹配')
        if (!response.ok) throw new BackendError(response.error?.code ?? 'ERROR', response.error?.message ?? '操作失败', response.error?.details)
        if (!response.result || typeof response.result !== 'object' || Array.isArray(response.result)) throw new Error('后台响应无效')
        this.pending.delete(pending.id)
        clearTimeout(pending.timer)
        pending.resolve(response.result)
      }
    } catch (error) { this.fail(error as Error, 'response') }
  }
}
