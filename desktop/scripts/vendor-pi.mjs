import { cp, mkdir, readdir, readFile, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const source = resolve(desktop, '../ref/pi')
const target = join(desktop, 'vendor/pi')
const packages = ['telemetry', 'tui', 'codemode', 'mcp', 'ai', 'agent', 'coding-agent']
await mkdir(target, { recursive: true })
for (const name of ['LICENSE', 'tsconfig.base.json']) await cp(join(source, name), join(target, name))
for (const name of packages) {
  const destination = join(target, 'packages', name)
  await mkdir(destination, { recursive: true })
  for (const entry of await readdir(join(source, 'packages', name))) {
    if (['src', 'scripts', 'schemas', 'LICENSES', 'docs', 'package.json', 'tsconfig.build.json', 'README.md', 'CHANGELOG.md'].includes(entry)) {
      await cp(join(source, 'packages', name, entry), join(destination, entry), { recursive: true })
    }
  }
}
const revision = 'sha256-ffa3e98626230f378f80200debb736c4a2238b5f2ba246a92cfc7d8e19604678'
const response = await fetch(`https://pi.dev/api/models/revisions/${revision}?types=chat,image,classifier`)
if (!response.ok) throw new Error(`Model catalog: HTTP ${response.status}`)
const catalog = Buffer.from(await response.arrayBuffer())
if (`sha256-${createHash('sha256').update(catalog).digest('hex')}` !== revision) throw new Error('Model catalog checksum mismatch')
await writeFile(join(target, 'models.all.json'), catalog)
const files = {}
async function record(directory, prefix = '') {
  for (const item of await readdir(directory, { withFileTypes: true })) {
    if (['node_modules', 'dist', 'data', 'SOURCE.json', 'package-lock.json'].includes(item.name)) continue
    const name = prefix + item.name
    if (item.isDirectory()) await record(join(directory, item.name), name + '/')
    else files[name] = createHash('sha256').update(await readFile(join(directory, item.name))).digest('hex')
  }
}
await record(target)
await writeFile(join(target, 'SOURCE.json'), JSON.stringify({ source: 'ref/pi', version: '1.3.0', license: 'MIT', modelCatalogRevision: revision, files }, null, 2) + '\n')
