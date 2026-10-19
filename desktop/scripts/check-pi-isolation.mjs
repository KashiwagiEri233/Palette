import { cp, mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const source = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const directory = await mkdtemp(join(tmpdir(), 'palette-pi-isolation-'))
function run(command, args) {
  const result = spawnSync(command, args, { cwd: directory, stdio: 'inherit', shell: false })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`${command} failed (${result.status})`)
}
try {
  for (const entry of ['package.json', 'package-lock.json', 'tsconfig.json', 'electron.vite.config.ts', 'src', 'scripts', 'vendor']) {
    await cp(join(source, entry), join(directory, entry), { recursive: true, filter: path => !path.split(/[\\/]/).some(part => ['node_modules', 'dist'].includes(part)) })
  }
  run(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['ci', '--ignore-scripts'])
  run(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['run', 'build'])
  run(process.execPath, ['--input-type=module', '-e', 'import { SessionManager, ModelRuntime } from "@earendil-works/pi-coding-agent"; const credentials = { read: async () => undefined, list: async () => [], modify: async (_id, fn) => fn(undefined), delete: async () => {} }; const models = await ModelRuntime.create({ credentials, modelsPath: null, refreshOnCreate: false }); if (!models.getModels().length) throw new Error("Empty catalog"); if (!SessionManager.inMemory().getSessionId()) throw new Error("Session unavailable"); console.log("Isolated SDK startup passed");'])
} finally { await rm(directory, { recursive: true, force: true }) }
