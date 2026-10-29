import { randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import type { Credential, CredentialInfo, CredentialStore } from '@earendil-works/pi-ai'

export async function atomicJson(path: string, value: unknown): Promise<void> {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 })
  const temporary = `${path}.${randomUUID()}.tmp`
  try { await writeFile(temporary, JSON.stringify(value), { mode: 0o600 }); await rename(temporary, path) }
  catch (error) { await unlink(temporary).catch(() => {}); throw error }
}
export async function readJson<T>(path: string, fallback: T): Promise<T> {
  try { return JSON.parse(await readFile(path, 'utf8')) as T }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return fallback; throw error }
}
export interface SecretEncryption {
  available(): boolean
  encrypt(value: string): Buffer
  decrypt(value: Buffer): string
}

export class AgentCredentials implements CredentialStore {
  private queue: Promise<unknown> = Promise.resolve()
  private memory = new Map<string, Credential>()
  constructor(private readonly file: string, private readonly encryption: SecretEncryption) {}
  get persistent(): boolean { return this.encryption.available() }
  private serialize<T>(work: () => Promise<T>): Promise<T> {
    const result = this.queue.then(work)
    this.queue = result.catch(() => {})
    return result
  }
  private async load(): Promise<Record<string, string>> { return readJson(this.file, {}) }
  read(providerId: string): Promise<Credential | undefined> {
    return this.serialize(async () => {
      const cached = this.memory.get(providerId)
      if (cached) return cached
      const encrypted = (await this.load())[providerId]
      if (!encrypted || !this.persistent) return undefined
      try { return JSON.parse(this.encryption.decrypt(Buffer.from(encrypted, 'base64'))) as Credential }
      catch { throw new Error('无法解密 Agent 凭据，请重新配置 API Key') }
    })
  }
  async list(): Promise<readonly CredentialInfo[]> {
    const providers = new Set([...Object.keys(await this.load()), ...this.memory.keys()])
    const result: CredentialInfo[] = []
    for (const providerId of providers) if (await this.read(providerId)) result.push({ providerId, type: 'api_key' })
    return result
  }
  modify(providerId: string, fn: (current: Credential | undefined) => Promise<Credential | undefined>): Promise<Credential | undefined> {
    return this.serialize(async () => {
      const data = await this.load()
      const encoded = data[providerId]
      const previous = this.memory.get(providerId) ?? (encoded && this.persistent ? JSON.parse(this.encryption.decrypt(Buffer.from(encoded, 'base64'))) as Credential : undefined)
      const value = await fn(previous)
      if (!value) return previous
      if (this.persistent) { data[providerId] = this.encryption.encrypt(JSON.stringify(value)).toString('base64'); await atomicJson(this.file, data); this.memory.delete(providerId) }
      else this.memory.set(providerId, value)
      return value
    })
  }
  delete(providerId: string): Promise<void> {
    return this.serialize(async () => { const data = await this.load(); delete data[providerId]; await atomicJson(this.file, data); this.memory.delete(providerId) })
  }
  async redact(text: string): Promise<string> {
    for (const info of await this.list()) {
      const credential = await this.read(info.providerId)
      if (credential?.type === 'api_key' && credential.key) text = text.split(credential.key).join('[redacted]')
    }
    return text
  }
  async redactJson(value: unknown): Promise<unknown> {
    const keys: string[] = []
    for (const info of await this.list()) {
      const credential = await this.read(info.providerId)
      if (credential?.type === 'api_key' && credential.key) keys.push(credential.key)
    }
    const redact = (item: unknown): unknown => {
      if (typeof item === 'string') return keys.reduce((text, key) => text.split(key).join('[redacted]'), item)
      if (Array.isArray(item)) return item.map(redact)
      if (item && typeof item === 'object') {
        const image = 'type' in item && item.type === 'image' && 'mimeType' in item && typeof item.mimeType === 'string'
        return Object.fromEntries(Object.entries(item).map(([key, entry]) => [key, image && key === 'data' ? entry : redact(entry)]))
      }
      return item
    }
    return redact(value)
  }
}
