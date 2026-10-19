import { describe, it, expect } from 'vitest'
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'
import { ConversationRepository } from '../src/main/conversations'

async function fixture(work: (root: string, repository: ConversationRepository) => Promise<void>) {
  const temporaryRoot = resolve('..', '.runtime', 'conversation-tests')
  await mkdir(temporaryRoot, { recursive: true })
  const root = await mkdtemp(join(temporaryRoot, 'palette-conversation-test-'))
  try { await work(root, new ConversationRepository(root, 'config-a')) }
  finally {
    if (!resolve(root).startsWith(join(temporaryRoot, 'palette-conversation-test-'))) throw new Error('Unexpected cleanup path')
    await rm(root, { recursive: true, force: true })
  }
}

describe('conversation persistence', () => {
  it('retains VM ownership, drafts and archive state after reload, isolated by root', async () => fixture(async (root, repository) => {
    const created = await repository.create('vm-a', randomUUID(), '整理下载目录')
    const updated = await repository.update('vm-a', created.id, created.revision, { draft: '保留这个草稿', pinned: true, archived: true })
    expect(await new ConversationRepository(root, 'config-a').list()).toEqual([updated])
    expect(await new ConversationRepository(root, 'config-b').list()).toEqual([])
    expect(updated.vm_id).toBe('vm-a')
  }))

  it('deduplicates concurrent creation and rejects request reuse on another VM', async () => fixture(async (_root, repository) => {
    const request = randomUUID()
    const [first, second] = await Promise.all([repository.create('vm-a', request), repository.create('vm-a', request)])
    expect(first.id).toBe(second.id)
    expect(await repository.list()).toHaveLength(1)
    await expect(repository.create('vm-b', request)).rejects.toThrow('不匹配')
  }))

  it('rejects stale revisions, foreign VMs, oversized drafts and ownership patches', async () => fixture(async (_root, repository) => {
    const item = await repository.create('vm-a', randomUUID())
    await repository.update('vm-a', item.id, 1, { title: '新的标题' })
    await expect(repository.update('vm-a', item.id, 1, { draft: '过时更新' })).rejects.toThrow('已更新')
    await expect(repository.update('vm-b', item.id, 2, { title: '错误归属' })).rejects.toThrow('不属于')
    await expect(repository.update('vm-a', item.id, 2, { vm_id: 'vm-b' })).rejects.toThrow('无效')
    await expect(repository.update('vm-a', item.id, 2, { draft: 'a'.repeat(16001) })).rejects.toThrow('无效')
    await expect(repository.update('vm-a', item.id, 2, { title: '' })).rejects.toThrow('无效')
    expect((await repository.list())[0].title).toBe('新的标题')
  }))

  it('serializes conflicting mutations and recovers its queue after rejection', async () => fixture(async (_root, repository) => {
    const item = await repository.create('vm-a', randomUUID())
    const results = await Promise.allSettled([repository.update('vm-a', item.id, 1, { title: '一个写入' }), repository.update('vm-a', item.id, 1, { title: '冲突写入' })])
    expect(results.map(result => result.status)).toEqual(['fulfilled', 'rejected'])
    await repository.update('vm-a', item.id, 2, { pinned: true })
    expect((await repository.list())[0].revision).toBe(3)
  }))

  it('deletes only owned records and removes VM conversations without affecting another VM', async () => fixture(async (_root, repository) => {
    const first = await repository.create('vm-a', randomUUID())
    await repository.create('vm-a', randomUUID())
    const other = await repository.create('vm-b', randomUUID())
    await expect(repository.remove('vm-b', first.id, 1)).rejects.toThrow('不属于')
    await repository.removeVm('vm-a')
    expect(await repository.list()).toEqual([other])
    await repository.remove('vm-b', other.id, 1)
    expect(await repository.list()).toEqual([])
  }))

  it('refuses malformed persistence without overwriting the original data', async () => fixture(async (root, repository) => {
    await repository.create('vm-a', randomUUID())
    const [directory] = await readdir(join(root, 'workspaces'))
    const file = join(root, 'workspaces', directory, 'conversations.json')
    for (const invalid of ['{', 'null', '{"version":1,"conversations":[null],"requests":{}}']) {
      await writeFile(file, invalid)
      await expect(repository.create('vm-a', randomUUID())).rejects.toThrow('对话记录无法读取')
      expect(await readFile(file, 'utf8')).toBe(invalid)
    }
  }))
})
