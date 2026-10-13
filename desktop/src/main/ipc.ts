import { connect } from 'node:net'
import { randomBytes } from 'node:crypto'
import { readFile, lstat } from 'node:fs/promises'

interface Endpoint { address: string; generation: string; token: string; transport: string; operation_timeout_seconds: number }

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

export async function call(runtimeRoot: string, method: string, vmId?: string, payload: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  const endpoint = await readEndpoint(runtimeRoot)
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
        if (!response.ok) throw new Error(`${response.error?.code ?? 'ERROR'}: ${response.error?.message ?? '操作失败'}`)
        done = true; resolve(response.result); socket.end()
      } catch (error) { fail(error as Error) }
    })
    socket.on('error', fail)
    socket.on('close', () => { if (!done) fail(new Error('后台连接中断，操作结果未知；请先检查状态')) })
  })
}
