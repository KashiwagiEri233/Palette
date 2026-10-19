import { cp, mkdir, readFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../vendor/pi')
const require = createRequire(join(root, 'package.json'))
function run(args, cwd = root) {
  const result = spawnSync(process.execPath, args, { cwd, stdio: 'inherit' })
  if (result.error) throw result.error
  if (result.status !== 0) process.exit(result.status ?? 1)
}
const metadata = JSON.parse(await readFile(join(root, 'SOURCE.json'), 'utf8'))
await Promise.all(Object.entries(metadata.files).map(async ([path, expected]) => {
  const actual = createHash('sha256').update(await readFile(join(root, path))).digest('hex')
  if (actual !== expected) throw new Error(`Pi source checksum mismatch: ${path}`)
}))
const catalog = await readFile(join(root, 'models.all.json'))
if (`sha256-${createHash('sha256').update(catalog).digest('hex')}` !== metadata.modelCatalogRevision) throw new Error('Pi model catalog checksum mismatch')
run(['packages/ai/scripts/hydrate-model-catalog.ts', 'models.all.json'])
const compiler = join(dirname(require.resolve('typescript/package.json')), 'bin/tsc')
for (const name of ['telemetry', 'tui', 'codemode', 'mcp', 'ai', 'agent', 'coding-agent']) {
  console.log(`Building pi/${name}`)
  const directory = join(root, 'packages', name)
  run([compiler, '-p', 'tsconfig.build.json'], directory)
  if (name === 'ai') await cp(join(directory, 'src/providers/data'), join(directory, 'dist/providers/data'), { recursive: true })
  if (name === 'coding-agent') {
    for (const path of ['modes/interactive/theme', 'modes/interactive/assets', 'core/export-html']) {
      await mkdir(join(directory, 'dist', path), { recursive: true })
      await cp(join(directory, 'src', path), join(directory, 'dist', path), { recursive: true, filter: path => !path.endsWith('.ts') })
    }
  }
}
