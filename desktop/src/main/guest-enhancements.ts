import { open, rename, unlink } from 'node:fs/promises'
import { dirname, join, basename } from 'node:path'
import { randomBytes } from 'node:crypto'

const CHUNK = 48 * 1024
export const MAX_FILE = 16 * 1024 * 1024
type Call = (method: string, payload: Record<string, unknown>) => Promise<Record<string, unknown>>

export function checkedGuestPath(value: unknown): string {
  if (typeof value !== 'string' || !value.trim() || value.length > 4096 || /[\0\r\n]/.test(value)) throw new Error('请输入客户机内的完整文件路径')
  if (!value.startsWith('/') && !/^[a-z]:[\\/]/i.test(value)) throw new Error('客户机文件路径必须是绝对路径')
  return value
}
export function checkedClipboard(text: string): string {
  if (Buffer.byteLength(text, 'utf8') > CHUNK || text.includes('\0')) throw new Error('剪贴板文本超过 48 KiB 或包含空字符')
  return text
}
function check(signal: AbortSignal): void { if (signal.aborted) throw new Error('操作已取消；客户机中可能保留未完成的文件') }

export async function withGuestLease<T>(call: Call, instanceId: string, signal: AbortSignal, operation: (request: Call) => Promise<T>): Promise<T> {
  check(signal)
  const lease = await call('control.acquire', { instance_id: instanceId })
  const payload = { instance_id: instanceId, lease_id: lease.lease_id }
  let heartbeatFailed = false
  const heartbeat = setInterval(() => {
    if (!signal.aborted) void call('control.heartbeat', { lease_id: lease.lease_id }).catch(() => { heartbeatFailed = true })
  }, 1000)
  const request: Call = (method, data) => {
    check(signal)
    if (heartbeatFailed) throw new Error('虚拟机控制权已丢失')
    return call(method, { ...data, ...payload })
  }
  try { return await operation(request) }
  finally { clearInterval(heartbeat); await call('control.release', { lease_id: lease.lease_id }) }
}

export async function uploadGuestFile(call: Call, hostPath: string, guestPath: string, signal: AbortSignal, progress: (bytes: number) => void): Promise<void> {
  checkedGuestPath(guestPath)
  const file = await open(hostPath, 'r')
  let transferId: unknown
  try {
    const stat = await file.stat()
    if (!stat.isFile() || stat.size > MAX_FILE) throw new Error('仅支持不超过 16 MiB 的普通文件')
    transferId = (await call('guest.file.open', { path: guestPath, mode: 'w' })).transfer_id
    const buffer = Buffer.alloc(CHUNK)
    let total = 0
    while (true) {
      check(signal)
      const { bytesRead } = await file.read(buffer)
      if (!bytesRead) break
      total += bytesRead
      if (total > MAX_FILE) throw new Error('文件已超过 16 MiB')
      const result = await call('guest.file.write', { transfer_id: transferId, data_base64: buffer.subarray(0, bytesRead).toString('base64') })
      if (result.count !== bytesRead) throw new Error('客户机未完整写入文件，请检查目标文件')
      progress(total)
    }
    await call('guest.file.close', { transfer_id: transferId }); transferId = undefined
  } finally {
    await file.close()
    if (transferId) await call('guest.file.close', { transfer_id: transferId }).catch(() => undefined)
  }
}

export async function downloadGuestFile(call: Call, hostPath: string, guestPath: string, signal: AbortSignal, progress: (bytes: number) => void): Promise<void> {
  checkedGuestPath(guestPath)
  const temporary = join(dirname(hostPath), `.${basename(hostPath)}.${randomBytes(8).toString('hex')}.partial`)
  const file = await open(temporary, 'wx', 0o600)
  let transferId: unknown
  let complete = false
  try {
    transferId = (await call('guest.file.open', { path: guestPath, mode: 'r' })).transfer_id
    let total = 0
    while (true) {
      check(signal)
      const result = await call('guest.file.read', { transfer_id: transferId, count: CHUNK })
      if (typeof result.data_base64 !== 'string' || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(result.data_base64)) throw new Error('客户机文件块无效')
      const data = Buffer.from(result.data_base64, 'base64')
      total += data.length
      if (data.length > CHUNK || result.count !== data.length || total > MAX_FILE || (!data.length && !result.eof)) throw new Error('文件块无效或文件超过 16 MiB')
      let offset = 0
      while (offset < data.length) { const result = await file.write(data, offset); if (!result.bytesWritten) throw new Error('宿主文件未完整写入'); offset += result.bytesWritten }
      progress(total)
      if (result.eof) break
    }
    await call('guest.file.close', { transfer_id: transferId }); transferId = undefined
    await file.sync(); await file.close()
    check(signal)
    await rename(temporary, hostPath)
    complete = true
  } finally {
    await file.close().catch(() => undefined)
    if (transferId) await call('guest.file.close', { transfer_id: transferId }).catch(() => undefined)
    if (!complete) await unlink(temporary).catch(() => undefined)
  }
}
