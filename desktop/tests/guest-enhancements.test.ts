import { afterEach, describe, expect, it, vi } from 'vitest'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { checkedClipboard, checkedGuestPath, uploadGuestFile, downloadGuestFile, withGuestLease } from '../src/main/guest-enhancements'

const folders: string[] = []
async function folder(): Promise<string> { const path = await mkdtemp(join(tmpdir(), 'palette-guest-test-')); folders.push(path); return path }
afterEach(async () => { for (const path of folders.splice(0)) await rm(path, { recursive: true, force: true }) })

describe('guest integration and host file boundaries', () => {
  it('validates UTF-8 bytes rather than character count and requires guest absolute paths', () => {
    expect(checkedClipboard('中文\n🖼️')).toBe('中文\n🖼️')
    expect(() => checkedClipboard('中'.repeat(20000))).toThrow('48 KiB')
    expect(() => checkedClipboard('a\0b')).toThrow()
    expect(checkedGuestPath('/home/user/example.txt')).toContain('/home')
    expect(checkedGuestPath('C:\\Users\\user\\example.txt')).toContain('C:')
    expect(() => checkedGuestPath('../host.txt')).toThrow()
  })

  it('uploads binary files in bounded chunks and closes the guest handle', async () => {
    const path = join(await folder(), 'input.bin')
    const data = Buffer.alloc(100000, 0xa5); await writeFile(path, data)
    const blocks: Buffer[] = []
    const request = vi.fn(async (method: string, payload: Record<string, unknown>) => {
      if (method === 'guest.file.open') return { transfer_id: 'opaque' }
      if (method === 'guest.file.write') { const block = Buffer.from(payload.data_base64 as string, 'base64'); blocks.push(block); return { count: block.length } }
      return {}
    })
    await uploadGuestFile(request, path, '/home/user/input.bin', new AbortController().signal, () => undefined)
    expect(Buffer.concat(blocks)).toEqual(data)
    expect(blocks.every(block => block.length <= 49152)).toBe(true)
    expect(request.mock.calls.at(-1)?.[0]).toBe('guest.file.close')
  })

  it('does not overwrite the host destination after an interrupted download', async () => {
    const root = await folder(), destination = join(root, 'output.txt')
    await writeFile(destination, 'original')
    let count = 0
    const request = vi.fn(async (method: string) => {
      if (method === 'guest.file.open') return { transfer_id: 'opaque' }
      if (method === 'guest.file.read') { if (count++) throw new Error('connection lost'); return { data_base64: Buffer.from('part').toString('base64'), count: 4, eof: false } }
      return {}
    })
    await expect(downloadGuestFile(request, destination, '/home/user/output.txt', new AbortController().signal, () => undefined)).rejects.toThrow('connection lost')
    expect(await readFile(destination, 'utf8')).toBe('original')
    expect(await readdir(root)).toEqual(['output.txt'])
    expect(request.mock.calls.at(-1)?.[0]).toBe('guest.file.close')
  })

  it('atomically publishes a completed download, including empty files', async () => {
    const destination = join(await folder(), 'empty.txt')
    const request = vi.fn(async (method: string) => method === 'guest.file.open' ? { transfer_id: 'opaque' } : method === 'guest.file.read' ? { data_base64: '', count: 0, eof: true } : {})
    await downloadGuestFile(request, destination, '/home/user/empty.txt', new AbortController().signal, () => undefined)
    expect((await readFile(destination)).length).toBe(0)
  })

  it('binds operations to the original instance and releases ownership after failure', async () => {
    const request = vi.fn(async (method: string) => method === 'control.acquire' ? { lease_id: 'token' } : {})
    await expect(withGuestLease(request, 'original-instance', new AbortController().signal, async controlled => { await controlled('guest.sync_time', {}); throw new Error('failure') })).rejects.toThrow('failure')
    expect(request.mock.calls[1]).toEqual(['guest.sync_time', { instance_id: 'original-instance', lease_id: 'token' }])
    expect(request.mock.calls.at(-1)?.[0]).toBe('control.release')
  })
})
