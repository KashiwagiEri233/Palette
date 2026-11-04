/** Explicit real-guest benchmark. Never starts, stops or edits VM definitions. */
import { _electron as electron } from '@playwright/test'
import { parseArgs } from 'node:util'
import { writeFile, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { platform, arch, hostname, cpus, totalmem, release } from 'node:os'

const { values: args } = parseArgs({ options: Object.fromEntries([
  ['root'], ['vm-id'], ['provider'], ['width'], ['height'], ['guest'], ['guest-arch'], ['accelerator'], ['output'], ['variant'],
  ['marker-x', '0'], ['marker-y', '0'], ['samples', '100'], ['rounds', '3'], ['motion-hz', '60'], ['idle-seconds', '10,120'], ['wake-samples', '1'],
].map(([name, fallback]) => [name, { type: 'string', ...(fallback ? { default: fallback } : {}) }])) })
for (const key of ['root', 'vm-id', 'provider', 'width', 'height', 'guest', 'guest-arch', 'accelerator', 'output']) {
  if (!args[key]) throw new Error(`Missing --${key}; see docs/display-experiment.md`)
}
if (!['rfb', 'raw'].includes(args.provider)) throw new Error('--provider must be rfb or raw')
const variant = args.variant ?? (args.provider === 'raw' ? 'raw' : 'adaptive')
if (!['adaptive', 'steady', 'raw'].includes(variant) || (variant === 'raw') !== (args.provider === 'raw')) throw new Error('Use --variant adaptive|steady with rfb, or raw with raw')
const width = Number(args.width), height = Number(args.height), samples = Number(args.samples), rounds = Number(args.rounds)
const x = Number(args['marker-x']), y = Number(args['marker-y']), motionHz = Number(args['motion-hz']), wakeSamples = Number(args['wake-samples'])
const idleSeconds = args['idle-seconds'].split(',').map(Number)
if (![width, height, samples, rounds, x, y, wakeSamples, ...idleSeconds].every(Number.isInteger) || samples < 1 || samples > 10000 || rounds < 1 || rounds > 20 || wakeSamples < 1 || wakeSamples > 20 || width < 640 || height < 200 || x < 0 || y < 0 || x + 640 > width || y + 160 > height || !Number.isFinite(motionHz) || motionHz < 1 || motionHz > 125 || idleSeconds.length !== new Set(idleSeconds).size || idleSeconds.some(n => n < 1 || n > 300)) throw new Error('Invalid benchmark dimensions, rate or counts')
const motionColumns = Math.floor((width - x - 160) / 4), motionRows = Math.max(1, Math.floor((height - y - 160) / 4))
if (samples > motionColumns * motionRows) throw new Error('Too many samples for unique coordinate markers at this resolution')
const sleep = ms => new Promise(resolveWait => setTimeout(resolveWait, Math.max(0, ms)))
const application = await electron.launch({ args: [resolve('desktop/out/main/index.js')], env: { ...process.env, PALETTE_ROOT: resolve(args.root) } })
const report = { schema_version: 2, kind: 'desktop-display', started_at: new Date().toISOString(), host: { os: platform(), arch: arch(), name: hostname(), release: release(), cpu_model: cpus()[0]?.model, logical_cpus: cpus().length, memory_bytes: totalmem() }, guest: args.guest, guest_arch: args['guest-arch'], accelerator: args.accelerator, provider: args.provider, variant, width, height, protocol: { samples, rounds, motion_hz: motionHz, idle_seconds: idleSeconds, wake_samples: wakeSamples }, resource_units: { cpu: 'one_core_percent', rss: 'bytes', clock: 'node_monotonic_ms' }, rounds: [], complete: false }
let page, powerBlocker
const redact = value => String(value)
  .replace(/\bBearer\s+[^\s"'<>]+/gi, 'Bearer [redacted]')
  .replace(/\bsk-[A-Za-z0-9_-]{12,}/g, '[redacted-key]')
  .replace(/((?:https?|wss?):\/\/)[^/\s:@]+:[^/\s@]+@/gi, '$1[redacted]@')
  .replace(/((?:ticket|token|password|api[_-]?key|secret)["']?\s*[:=]\s*["']?)[^\s"'&,;<>]+/gi, '$1[redacted]')
const redactUi = value => typeof value === 'string' ? redact(value) : Array.isArray(value) ? value.map(redactUi) : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redactUi(item)])) : value
try {
  page = await application.firstWindow()
  await page.locator('.global-nav-rail .status-dot.online').waitFor({ timeout: 30000 })
  const vm = await page.evaluate(async id => {
    const { vms } = await window.palette.request('vms.list')
    const item = vms.find(vm => vm.vm_id === id)
    if (!item || item.status.state !== 'RUNNING') throw new Error('A disposable benchmark VM must already be running')
    return item
  }, args['vm-id'])
  if (vm.status.guest_arch !== args['guest-arch'] || vm.status.accelerator !== args.accelerator) throw new Error('Execution metadata does not match the running VM')
  const metrics = await page.evaluate(id => window.palette.request('control.metrics', id), args['vm-id'])
  if (!metrics.execution?.qemu_version) throw new Error('Backend lacks benchmark execution metadata')
  report.execution = metrics.execution
  report.vm_id = args['vm-id']; report.instance_id = vm.status.instance_id
  report.observation_pipeline = metrics.observation_pipeline
  if (!['serial', 'split'].includes(report.observation_pipeline)) throw new Error('Backend lacks observation-pipeline metadata')
  await page.getByRole('button', { name: '虚拟机管理', exact: true }).click()
  await page.getByRole('button', { name: vm.name, exact: true }).click()
  await page.getByRole('tab', { name: '控制台', exact: true }).click()
  await page.locator('.console-surface canvas').waitFor({ timeout: 30000 })
  powerBlocker = await application.evaluate(({ BrowserWindow, powerSaveBlocker }, size) => {
    const window = BrowserWindow.getAllWindows()[0]
    window.setSize(size.width + 640, size.height + 320)
    window.focus()
    globalThis.displayBenchmarkFocus = []
    for (const event of ['blur', 'focus', 'hide', 'minimize', 'restore']) window.on(event, () => globalThis.displayBenchmarkFocus.push({ at: Date.now(), event, focused: window.isFocused(), visible: window.isVisible() }))
    return powerSaveBlocker.start('prevent-display-sleep')
  }, { width, height })
  const actual = await page.locator('.console-surface canvas').evaluate(canvas => ({ width: canvas.width, height: canvas.height, provider: canvas.dataset.paletteDisplay }))
  if (actual.width !== width || actual.height !== height || actual.provider !== args.provider) throw new Error(`Unexpected active display: ${JSON.stringify(actual)}`)
  report.display_settings = await page.evaluate(() => window.palette.request('display.settings.get'))
  const active = report.display_settings.instances.find(item => item.vm_id === args['vm-id'])
  if (!active || active.instance_id !== vm.status.instance_id || active.provider !== args.provider || active.refresh_mode !== (variant === 'adaptive' ? 'adaptive' : 'steady')) throw new Error(`Actual display mode does not match --variant: ${JSON.stringify(active)}`)
  await page.evaluate(({ x, y, width, height }) => {
    const canvas = document.querySelector('.console-surface canvas'), surface = document.querySelector('.console-surface')
    const context = canvas.getContext('2d', { willReadFrequently: true })
    if (!context) throw new Error('Canvas readback unavailable')
    const state = window.displayProbe = { ack: [], observation: [], stop: false, failure: null, phase: null, key: null, motion: null }
    state.read = (slot = 0) => {
      const pixels = context.getImageData(x + slot * 128, y, 128, 8).data
      let value = 0
      for (let bit = 0; bit < 32; bit++) {
        const at = (2 * 128 + bit * 4 + 2) * 4
        const bright = pixels[at] > 200 && pixels[at + 1] > 200 && pixels[at + 2] > 200
        const dark = pixels[at] < 40 && pixels[at + 1] < 40 && pixels[at + 2] < 40
        if (!bright && !dark) return null
        if (bright) value = (value | (1 << bit)) >>> 0
      }
      const sequence = (value >>> 8) & 65535
      return (value & 255) === 0xa5 && (value >>> 24) === ((~sequence) & 255) ? sequence : null
    }
    const target = () => {
      if (state.phase !== 'drag') return state.read(4)
      const px = state.read(2), py = state.read(3)
      return px === null || py === null ? null : `${px},${py}`
    }
    state.armKey = () => {
      if (!surface.classList.contains('captured')) throw new Error('Operator control was lost before input')
      surface.focus()
      const previous = state.read()
      if (previous === null || state.failure) throw new Error(state.failure ?? 'Guest marker lost')
      state.key = { expected: (previous + 1) & 65535, at: null, armedAt: performance.now(), visible: null }
    }
    surface.addEventListener('keydown', event => {
      if (event.code === 'Space' && !event.repeat && state.key && state.key.at === null) state.key.at = performance.now()
    }, true)
    surface.addEventListener('pointermove', event => {
      if (state.phase !== 'drag' || !(event.buttons & 1) || !state.motion) return
      const rect = canvas.getBoundingClientRect()
      const px = Math.max(0, Math.min(width - 1, Math.floor((event.clientX - rect.left) * width / rect.width)))
      const py = Math.max(0, Math.min(height - 1, Math.floor((event.clientY - rect.top) * height / rect.height)))
      const item = { id: state.motion.issued.length, at: performance.now(), target: `${Math.floor((px - x) / 4)},${Math.floor((py - y) / 4)}`, x: px, y: py }
      state.motion.issued.push(item)
    }, true)
    surface.addEventListener('wheel', event => {
      if (state.phase !== 'scroll' || !state.motion) return
      const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? height : 1)
      state.motion.hostPixels += pixels
      const steps = Math.trunc(state.motion.hostPixels / 100)
      state.motion.issued.push({ id: state.motion.issued.length, at: performance.now(), target: (state.motion.base + state.wheelUnit * steps) & 65535 })
    }, true)
    window.addEventListener('palette-input-timing', event => state.ack.push(event.detail))
    const watch = () => {
      if (state.stop) return
      const now = performance.now(), key = state.key
      if (key && key.at !== null && key.visible === null && state.read() === key.expected) key.visible = now
      const motion = state.motion
      if (motion && motion.issued.length) {
        const visible = target()
        const sequence = state.read(1)
        if (visible !== null && sequence !== null && (visible !== motion.previous || sequence !== motion.previousMotion)) {
          motion.previous = visible
          motion.previousMotion = sequence
          const matched = motion.issued.findLast(item => item.target === visible)
          if (matched && !matched.visible) {
            matched.visible = now
            motion.visible.push({ id: matched.id, input_at: matched.at, visible_at: now, latency_ms: now - matched.at })
            if (motion.lastVisible !== null) motion.intervals.push(now - motion.lastVisible)
            motion.lastVisible = now
          }
        }
        const current = motion.issued.findLast(item => item.target === visible), latest = motion.issued.at(-1)
        if (current) {
          motion.staleness.push(now - current.at)
          if (state.phase === 'drag') motion.targetLag.push(Math.hypot(latest.x - current.x, latest.y - current.y))
        }
      }
      requestAnimationFrame(watch)
    }
    requestAnimationFrame(watch)
  }, { x, y, width, height })
  await page.evaluate(() => {
    const state = window.displayProbe
    state.focusEvents = []
    for (const name of ['blur', 'focus', 'visibilitychange', 'palette-control-lost']) {
      window.addEventListener(name, event => state.focusEvents.push({ at: performance.now(), event: name, visible: document.visibilityState, focused: document.hasFocus(), captured: !!document.querySelector('.console-surface.captured'), error: event.detail?.error }))
    }
  })
  // A negotiated canvas size can precede the first RFB paint. Readiness is outside
  // the measured input/resource windows and does not send an input to wake VNC.
  await page.waitForFunction(() => [0, 1, 2, 3, 4].every(slot => window.displayProbe.read(slot) !== null), undefined, { timeout: 30000, polling: 'raf' })
  await page.getByRole('button', { name: '接管', exact: true }).click()
  await page.getByText('人工控制', { exact: true }).waitFor()
  const resources = async () => {
    const started = performance.now()
    const backend = await page.evaluate(id => window.palette.request('control.metrics', id), args['vm-id'])
    const desktop = await application.evaluate(({ app }) => app.getAppMetrics().map(item => ({ pid: item.pid, created_at: item.creationTime, cpu_seconds: item.cpu.cumulativeCPUUsage, rss_bytes: item.memory.workingSetSize * 1024 })))
    const processes = [...backend.resources.processes, ...desktop]
    return {
      at: (started + performance.now()) / 2,
      incomplete: backend.resources.incomplete || processes.some(item => !Number.isFinite(item.cpu_seconds) || !Number.isFinite(item.rss_bytes)),
      processes,
      input_transport: backend.input_transport ?? 'unavailable',
      backend_timings: { input: backend.input, observation: backend.observation, runtime: backend.runtime, display: backend.display, observation_pipeline: backend.observation_pipeline, display_settings: backend.display_settings }
    }
  }
  const keySample = async () => {
    await page.evaluate(() => window.displayProbe.armKey())
    await page.keyboard.press('Space')
    await page.waitForFunction(() => typeof window.displayProbe.key?.visible === 'number' && typeof window.displayProbe.key?.at === 'number', undefined, { timeout: 5000 })
    return page.evaluate(() => { const key = window.displayProbe.key; return { id: key.expected, input_at: key.at, visible_at: key.visible, latency_ms: key.visible - key.at } })
  }
  const box = await page.locator('.console-surface canvas').boundingBox()
  if (!box) throw new Error('Display disappeared')
  const center = { x: box.x + box.width / 2, y: box.y + box.height / 2 }
  const startMotion = phase => page.evaluate(phase => {
    const state = window.displayProbe
    state.phase = phase
    state.motion = { issued: [], visible: [], intervals: [], staleness: [], targetLag: [], previous: phase === 'drag' ? `${state.read(2)},${state.read(3)}` : state.read(4), previousMotion: state.read(1), lastVisible: null, base: state.read(4), hostPixels: 0 }
  }, phase)
  const finishMotion = async () => {
    await page.waitForFunction(() => {
      const { motion, phase, read } = window.displayProbe
      if (!motion?.issued.length) return false
      const target = phase === 'drag' ? `${read(2)},${read(3)}` : read(4)
      return motion.issued.at(-1).target === target && motion.issued.at(-1).visible
    }, undefined, { timeout: 10000 })
    return page.evaluate(() => {
      const state = window.displayProbe, motion = state.motion
      state.phase = null; state.motion = null
      const last = motion.issued.at(-1)
      return { input_to_visible_ms: motion.visible.map(item => item.latency_ms), samples: motion.visible, issued: motion.issued, frame_intervals_ms: motion.intervals, staleness_ms: motion.staleness, target_lag_px: motion.targetLag, settle_ms: last.visible - last.at, merged_or_not_presented: motion.issued.length - motion.visible.length }
    })
  }
  const calibrateWheel = async () => {
    await page.mouse.move(center.x, center.y)
    const previous = await page.evaluate(() => window.displayProbe.read(4))
    await page.mouse.wheel(0, 100)
    await page.waitForFunction(previous => { const value = window.displayProbe.read(4); return typeof value === 'number' && value !== previous }, previous, { timeout: 5000 })
    return page.evaluate(({ previous, samples }) => {
      const state = window.displayProbe, delta = ((state.read(4) - previous + 32768) & 65535) - 32768
      if (delta <= 0 || delta > 10000) throw new Error('Guest wheel calibration failed')
      let a = delta, b = 65536
      while (b) { const next = a % b; a = b; b = next }
      if (samples > 65536 / a) throw new Error('Too many samples for unique calibrated wheel markers')
      state.wheelUnit = delta
      return delta
    }, { previous, samples })
  }
  // Startup and power-management commands can leave another guest window focused.
  // Markers establish that this visible center belongs to the disposable fixture.
  // These acknowledged preparation actions are excluded from every measured window.
  await page.mouse.click(center.x, center.y, { delay: 40 })
  report.preparation = { measured: false, focus_click: { x: width / 2, y: height / 2, hold_ms: 40 }, key_calibration: await keySample() }
  for (const load of ['idle', 'observe']) for (let round = 1; round <= rounds; round++) {
    await page.evaluate(({ id, load }) => {
      const state = window.displayProbe
      state.ack = []; state.observation = []; state.failure = null; state.loadStop = false
      state.observer = load === 'observe' ? (async () => {
        while (!state.loadStop) {
          try {
            const at = performance.now()
            await window.palette.request('display.sync', id)
            state.observation.push(performance.now() - at)
          } catch (error) { state.failure = String(error); break }
        }
      })() : Promise.resolve()
    }, { id: args['vm-id'], load })
    const resourceSamples = [await resources()], input = [], motion = {}
    for (let index = 0; index < samples; index++) {
      input.push(await keySample())
      if (index % 10 === 0) resourceSamples.push(await resources())
    }
    const wheelUnit = await calibrateWheel()
    for (const phase of ['drag', 'scroll']) {
      await page.mouse.move(center.x, center.y)
      if (phase === 'drag') await page.mouse.down()
      await startMotion(phase)
      const start = Date.now()
      for (let step = 0; step < samples; step++) {
        await sleep(start + step * 1000 / motionHz - Date.now())
        if (phase === 'drag') {
          // Unique bucket-centered native coordinates, safely away from the marker.
          const px = x + 4 * (20 + step % motionColumns) + 2
          const py = y + 4 * (20 + Math.floor(step / motionColumns) % motionRows) + 2
          await page.mouse.move(box.x + (px + .25) * box.width / width, box.y + (py + .25) * box.height / height)
        } else await page.mouse.wheel(0, 100)
        if (step % 10 === 0) resourceSamples.push(await resources())
      }
      motion[phase] = await finishMotion()
      if (phase === 'drag') await page.mouse.up()
      const minimumMatches = samples >= 100 ? 10 : 1
      if (motion[phase].input_to_visible_ms.length < minimumMatches) throw new Error(`Insufficient matched ${phase} feedback`)
    }
    resourceSamples.push(await resources())
    report.pending_round = { round, load, numbered_inputs: input, drag: motion.drag, scroll: motion.scroll, resources: resourceSamples }
    // Static intervals are separately sampled; no event is sent to keep VNC awake.
    // Cycle key/drag/wheel across rounds so all three wake paths are covered.
    const idleWake = [], idleKind = ['key', 'drag', 'scroll'][(round - 1) % 3]
    for (const seconds of idleSeconds) for (let sample = 0; sample < wakeSamples; sample++) {
      await page.mouse.move(center.x, center.y)
      if (idleKind === 'drag') await page.mouse.down()
      const idleResources = [await resources()]
      await sleep(seconds * 1000)
      idleResources.push(await resources())
      let feedback
      if (idleKind === 'key') feedback = await keySample()
      else {
        await startMotion(idleKind)
        if (idleKind === 'drag') {
          const px = x + 4 * (20 + (sample + seconds) % motionColumns) + 2, py = y + 122
          await page.mouse.move(box.x + (px + .25) * box.width / width, box.y + (py + .25) * box.height / height)
        } else await page.mouse.wheel(0, 100)
        const result = await finishMotion()
        feedback = result.samples[0]
      }
      if (idleKind === 'drag') await page.mouse.up()
      idleWake.push({ idle_seconds: seconds, input_kind: idleKind, ...feedback, resources: idleResources })
    }
    await page.evaluate(async () => { const state = window.displayProbe; state.loadStop = true; await state.observer })
    const data = await page.evaluate(() => {
      const state = window.displayProbe
      if (state.failure) throw new Error(state.failure)
      return { ack_batches: state.ack, input_to_ack_ms: state.ack.map(item => item.queue_ms + item.roundtrip_ms), observation_ms: state.observation }
    })
    // Top up isolated observation samples after the measured input/resource window.
    for (let index = data.observation_ms.length; index < samples; index++) {
      data.observation_ms.push(await page.evaluate(async id => { const at = performance.now(); await window.palette.request('display.sync', id); return performance.now() - at }, args['vm-id']))
    }
    const shadow = []
    if (args.provider === 'raw') for (let index = 0; index < samples; index++) {
      shadow.push(await page.evaluate(async ({ id, instanceId }) => { const at = performance.now(); await window.palette.request('display.shadow', id, { instance_id: instanceId }); return performance.now() - at }, { id: args['vm-id'], instanceId: vm.status.instance_id }))
    }
    report.rounds.push({ round, load, numbered_inputs: input, input_to_visible_ms: input.map(item => item.latency_ms), ...data, shadow_observation_ms: shadow, drag: motion.drag, scroll: motion.scroll, drag_frame_intervals_ms: motion.drag.frame_intervals_ms, scroll_frame_intervals_ms: motion.scroll.frame_intervals_ms, idle_wake: idleWake, wheel_unit: wheelUnit, resources: resourceSamples })
    delete report.pending_round
    console.log(JSON.stringify({ round, load, variant, complete: true }))
  }
  await page.getByRole('button', { name: '结束控制', exact: true }).click()
  await page.waitForFunction(async id => {
    const { vms } = await window.palette.request('vms.list')
    const ledger = vms.find(vm => vm.vm_id === id)?.status.input_ledger
    return ledger && !ledger.keys.length && !ledger.buttons.length && ledger.release_confirmed
  }, args['vm-id'], { timeout: 5000, polling: 100 })
  const finalMetrics = await page.evaluate(id => window.palette.request('control.metrics', id), args['vm-id'])
  const finalDisplay = finalMetrics.display_settings
  if (finalMetrics.instance_id !== report.instance_id || finalMetrics.observation_pipeline !== report.observation_pipeline || !finalDisplay || finalDisplay.provider !== active.provider || finalDisplay.refresh_mode !== active.refresh_mode) throw new Error('Display instance or observation pipeline changed during measurement')
  report.complete = true
} catch (error) {
  report.error = redact(error); process.exitCode = 1
  // Release held input before diagnostic I/O, which may itself time out.
  await page?.evaluate(() => {
    const state = window.displayProbe
    if (!state) return
    state.failureProbe = { phase: state.phase, markers: [0, 1, 2, 3, 4].map(slot => state.read(slot)), key: state.key, last_motion_target: state.motion?.issued.at(-1), observer_error: state.failure, focus_events: state.focusEvents, focused: document.hasFocus(), captured: !!document.querySelector('.console-surface.captured'), active_element: document.activeElement?.className }
    state.stop = true; state.loadStop = true; state.phase = null
  }).catch(() => {})
  await page?.getByRole('button', { name: '结束控制', exact: true }).click({ timeout: 1000 }).catch(() => {})
  await mkdir(dirname(resolve(args.output)), { recursive: true })
  if (page) {
    try {
      const state = await page.evaluate(() => {
        const visible = selector => [...document.querySelectorAll(selector)].filter(element => element.getClientRects().length).map(element => element.textContent?.trim().slice(0, 500)).filter(Boolean)
        let probe = null
        try {
          const state = window.displayProbe
          if (state) probe = state.failureProbe ?? { phase: state.phase, markers: [0, 1, 2, 3, 4].map(slot => state.read(slot)), key: state.key, last_motion_target: state.motion?.issued.at(-1), observer_error: state.failure }
        } catch (error) { probe = { error: String(error) } }
        return { title: document.title, url: location.href, has_palette_api: !!window.palette, headings: visible('h1,h2'), messages: visible('[role="alert"],[role="status"],.console-placeholder,.desktop-error,.loading-state'), buttons: visible('button'), canvases: [...document.querySelectorAll('.console-surface canvas')].map(canvas => ({ width: canvas.width, height: canvas.height, provider: canvas.dataset.paletteDisplay })), probe }
      })
      report.failure_ui = redactUi(state)
      const path = resolve(args.output) + '.failure.png'
      await page.screenshot({ path, fullPage: false, mask: [page.locator('input,textarea,[contenteditable="true"],.agent-settings-fields,[role="alert"],.desktop-error')], timeout: 5000 })
      report.failure_screenshot = path
    } catch (diagnosticError) { report.failure_diagnostic_error = redact(diagnosticError) }
  }
} finally {
  report.native_focus_events = await application.evaluate(() => globalThis.displayBenchmarkFocus ?? []).catch(() => [])
  report.focus_events = await page?.evaluate(() => window.displayProbe?.focusEvents ?? []).catch(() => [])
  await page?.evaluate(() => { if (window.displayProbe) { window.displayProbe.stop = true; window.displayProbe.loadStop = true } }).catch(() => {})
  await page?.getByRole('button', { name: '结束控制', exact: true }).click({ timeout: 2000 }).catch(() => {})
  if (powerBlocker !== undefined) await application.evaluate(({ powerSaveBlocker }, id) => powerSaveBlocker.stop(id), powerBlocker).catch(() => {})
  await application.evaluate(({ app }) => app.exit(0)).catch(() => {})
  await application.close().catch(() => {})
  report.finished_at = new Date().toISOString()
  await mkdir(dirname(resolve(args.output)), { recursive: true })
  await writeFile(resolve(args.output), JSON.stringify(report, null, 2) + '\n')
  console.log(JSON.stringify({ report: resolve(args.output), complete: report.complete }))
}
