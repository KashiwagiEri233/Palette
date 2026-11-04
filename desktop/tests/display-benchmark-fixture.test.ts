import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { expect, it } from 'vitest'

type Rectangle = { x: number; y: number; width: number; height: number; color: string }
function fixture() {
  const handlers = new Map<string, Array<(event: Record<string, unknown>) => void>>()
  function drawing() {
    return {
      fillStyle: '', rectangles: [] as Rectangle[],
      fillRect(x: number, y: number, width: number, height: number) { this.rectangles.push({ x, y, width, height, color: this.fillStyle }) }
    }
  }
  const markers = drawing(), scene = drawing()
  const elements: Record<string, object> = {
    '#markers': { getContext: () => markers },
    '#scene': { width: 0, height: 0, getContext: () => scene },
    '#counter': { value: '0' }, '#fullscreen': {}
  }
  const html = readFileSync(new URL('../../guest/fixtures/display-latency.html', import.meta.url), 'utf8')
  const script = html.match(/<script>([\s\S]*)<\/script>/)?.[1]
  if (!script) throw new Error('Latency fixture script is missing')
  runInNewContext(script, {
    innerWidth: 1280, innerHeight: 800,
    document: { querySelector: (selector: string) => elements[selector], documentElement: { requestFullscreen() {} } },
    addEventListener: (name: string, handler: (event: Record<string, unknown>) => void) => handlers.set(name, [...handlers.get(name) ?? [], handler])
  }, { timeout: 1000 })
  return {
    scene,
    dispatch(name: string, fields: Record<string, unknown>) { for (const handler of handlers.get(name) ?? []) handler({ preventDefault() {}, ...fields }) },
    read(slot: number) {
      const bits = markers.rectangles.filter(rect => rect.x >= slot * 128 && rect.x < (slot + 1) * 128).slice(-32)
      if (bits.length !== 32) throw new Error('Incomplete marker')
      const word = bits.reduce((word, rect, bit) => rect.color === '#fff' ? word | (1 << bit) : word, 0) >>> 0
      const sequence = (word >>> 8) & 65535
      expect(word & 255).toBe(0xa5)
      expect(word >>> 24).toBe((~sequence) & 255)
      return sequence
    }
  }
}

it('paints real scrolling changes for 120-pixel detents and preserves cumulative coalesced motion', () => {
  const combined = fixture(), separate = fixture()
  const initial = combined.scene.rectangles.slice()
  combined.scene.rectangles = []
  combined.dispatch('wheel', { deltaY: 120, deltaMode: 0 })
  expect(combined.scene.rectangles).not.toEqual(initial)
  combined.scene.rectangles = []
  combined.dispatch('wheel', { deltaY: 240, deltaMode: 0 })
  for (let index = 0; index < 3; index++) {
    separate.scene.rectangles = []
    separate.dispatch('wheel', { deltaY: 120, deltaMode: 0 })
  }
  expect(combined.read(4)).toBe(360)
  expect(separate.read(4)).toBe(360)
  expect(combined.scene.rectangles).toEqual(separate.scene.rectangles)
})

it('encodes drag coordinates independently of event counts and ignores key auto-repeat', () => {
  const target = fixture()
  target.dispatch('pointerdown', {})
  target.dispatch('pointermove', { clientX: 82, clientY: 122 })
  target.dispatch('pointermove', { clientX: 482, clientY: 162 })
  expect(target.read(2)).toBe(120)
  expect(target.read(3)).toBe(40)
  target.dispatch('pointerup', {})
  target.dispatch('pointermove', { clientX: 100, clientY: 100 })
  expect(target.read(2)).toBe(120)
  target.dispatch('keydown', { code: 'Space', repeat: false })
  target.dispatch('keydown', { code: 'Space', repeat: true })
  expect(target.read(0)).toBe(1)
})

it('matches a repeated drag endpoint only after fresh guest motion feedback', () => {
  const source = readFileSync(new URL('../scripts/benchmark-display.mjs', import.meta.url), 'utf8')
  const start = source.indexOf('await page.evaluate(({ x, y, width, height }) => {')
  const end = source.indexOf('}, { x, y, width, height })', start)
  expect(start).toBeGreaterThan(0)
  expect(end).toBeGreaterThan(start)
  const initialize = source.slice(start + 'await page.evaluate('.length, end + 1)
  const markers = [0, 7, 24, 20, 100]
  const context = {
    getImageData(x: number) {
      const sequence = markers[x / 128]
      const word = (0xa5 | sequence << 8 | ((~sequence & 255) << 24)) >>> 0
      const data = new Uint8ClampedArray(128 * 8 * 4)
      for (let bit = 0; bit < 32; bit++) {
        const at = (2 * 128 + bit * 4 + 2) * 4
        data.fill(word & (1 << bit) ? 255 : 0, at, at + 3)
      }
      return { data }
    }
  }
  const canvas = { getContext: () => context }
  const surface = { addEventListener() {} }
  const window = { addEventListener() {}, displayProbe: undefined as unknown }
  let clock = 100, render = () => {}
  runInNewContext(`(${initialize})({ x: 0, y: 0, width: 1920, height: 1080 })`, {
    window, document: { querySelector: (selector: string) => selector.endsWith('canvas') ? canvas : surface },
    performance: { now: () => clock }, requestAnimationFrame: (callback: () => void) => { render = callback }
  }, { timeout: 1000 })
  const motion = {
    issued: [{ id: 0, at: 90, target: '24,20', x: 98, y: 82 }],
    visible: [] as Array<{ id: number; latency_ms: number }>, intervals: [], staleness: [], targetLag: [],
    previous: '24,20', previousMotion: 7, lastVisible: null
  }
  Object.assign(window.displayProbe as object, { phase: 'drag', motion })
  render()
  expect(motion.visible).toHaveLength(0)
  markers[1] = 8
  clock = 200
  render()
  expect(motion.visible).toMatchObject([{ id: 0, latency_ms: 110 }])
})
