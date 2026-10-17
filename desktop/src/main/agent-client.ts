import { connect } from 'node:net'
import { randomBytes } from 'node:crypto'
import { BackendError, call } from './ipc'

export interface AgentEndpoint { address: string; generation: string; token: string; operation_timeout_seconds: number }
export interface AgentReply { result: Record<string, unknown>; image: Buffer }

/** Each request owns its socket; cancellation never retries an unknown mutation. */
export class AgentClient {
  constructor(private readonly endpoint: AgentEndpoint) {}

  static async discover(runtimeRoot: string): Promise<AgentClient> {
    const hello = await call(runtimeRoot, 'hello')
    if (!Array.isArray(hello.features) || !hello.features.includes('driver-claim-v1')) throw new Error('后台需要升级以支持内置 Agent，请重新启动后台')
    const endpoint = hello.agent_endpoint as AgentEndpoint | undefined
    if (!endpoint || typeof endpoint.address !== 'string' || !/^[a-f0-9]{32}$/.test(endpoint.generation) || !/^[a-f0-9]{64}$/.test(endpoint.token) || !Number.isFinite(endpoint.operation_timeout_seconds)) throw new Error('Agent 连接信息无效')
    if (process.platform === 'win32' ? !endpoint.address.startsWith('\\\\.\\pipe\\palette-') : !endpoint.address.endsWith('.sock')) throw new Error('Agent 传输地址无效')
    return new AgentClient(endpoint)
  }

  request(method: string, vmId: string, parameters: Record<string, unknown> = {}, claimToken?: string, signal?: AbortSignal): Promise<AgentReply> {
    signal?.throwIfAborted()
    const requestId = randomBytes(16).toString('hex')
    const body = Buffer.from(JSON.stringify({ version: 1, generation: this.endpoint.generation, token: this.endpoint.token, request_id: requestId, claim_token: claimToken, command: { method, parameters: { ...parameters, vm_id: vmId } } }))
    if (body.length > 256 * 1024) throw new Error('Agent 请求过大')
    return new Promise((resolve, reject) => {
      const socket = connect(this.endpoint.address)
      let bytes = Buffer.alloc(0)
      let response: { ok: boolean; result: Record<string, unknown>; image_bytes: number; error?: { code: string; message: string } } | undefined
      let done = false
      const finish = (error?: Error, result?: AgentReply) => {
        if (done) return
        done = true
        signal?.removeEventListener('abort', abort)
        socket.destroy()
        if (error) reject(error)
        else resolve(result!)
      }
      const abort = () => finish(new Error('Agent 请求已取消，已发送操作的结果可能未知'))
      signal?.addEventListener('abort', abort, { once: true })
      socket.setTimeout(Math.min(3600_000, (this.endpoint.operation_timeout_seconds + 5) * 1000), () => finish(new Error('Agent 后台超时，操作结果未知')))
      socket.on('connect', () => { const header = Buffer.alloc(4); header.writeUInt32BE(body.length); socket.write(Buffer.concat([header, body])) })
      socket.on('data', chunk => {
        if (done) return
        bytes = Buffer.concat([bytes, chunk])
        try {
          if (!response && bytes.length >= 4) {
            const length = bytes.readUInt32BE(0)
            if (!length || length > 256 * 1024) throw new Error('Agent JSON 响应大小无效')
            if (bytes.length < length + 4) return
            const parsed = JSON.parse(bytes.subarray(4, length + 4).toString('utf8'))
            if (parsed.version !== 1 || parsed.request_id !== requestId || parsed.generation !== this.endpoint.generation) throw new Error('Agent 响应身份不匹配')
            if (typeof parsed.ok !== 'boolean' || !Number.isInteger(parsed.image_bytes) || parsed.image_bytes < 0 || parsed.image_bytes > 8 * 1024 * 1024) throw new Error('Agent 图片长度无效')
            response = parsed
            bytes = bytes.subarray(length + 4)
          }
          if (!response || bytes.length < 4) return
          const imageLength = bytes.readUInt32BE(0)
          if (imageLength !== response.image_bytes) throw new Error('Agent 图片长度不匹配')
          if (bytes.length < imageLength + 4) return
          if (bytes.length !== imageLength + 4) throw new Error('Agent 响应包含额外数据')
          if (!response.ok) throw new BackendError(response.error?.code ?? 'ERROR', response.error?.message ?? 'Agent 操作失败')
          if (!response.result || typeof response.result !== 'object' || Array.isArray(response.result)) throw new Error('Agent 结果无效')
          finish(undefined, { result: response.result, image: bytes.subarray(4) })
        } catch (error) { finish(error as Error) }
      })
      socket.on('error', error => finish(error))
      socket.on('close', () => { if (!done) finish(new Error('Agent 连接中断，操作结果未知')) })
      if (signal?.aborted) abort()
    })
  }
}
