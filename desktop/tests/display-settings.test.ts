import { describe, expect, it, vi } from 'vitest'
import type { DisplaySettings } from '../src/shared/types'
import { DisplaySettingsController } from '../src/renderer/lib/display-settings'

const inherited: DisplaySettings = { configured_mode: 'inherit', instances: [] }
const configured: DisplaySettings = {
  configured_mode: 'low_latency',
  instances: [{ vm_id: 'vm-test', instance_id: 'inst-old', provider: 'rfb', refresh_mode: 'adaptive', requested_mode: 'inherit', restart_required: true, reason: 'Requires a new VM instance' }]
}

describe('display settings', () => {

  it('preserves the confirmed switch value when saving fails', async () => {
    const request = vi.fn().mockResolvedValueOnce(inherited).mockRejectedValueOnce(new Error('Settings are not writable'))
    const controller = new DisplaySettingsController(request)
    await controller.load()
    await controller.update(true)
    expect(controller.snapshot()).toEqual({ settings: inherited, busy: false, saved: false, error: 'Settings are not writable' })
  })

  it('does not submit another toggle while a mutation is pending', async () => {
    let complete!: (settings: DisplaySettings) => void
    const request = vi.fn().mockResolvedValueOnce(inherited).mockImplementationOnce(() => new Promise<DisplaySettings>(resolve => { complete = resolve }))
    const controller = new DisplaySettingsController(request)
    await controller.update(true)
    expect(request).not.toHaveBeenCalled()
    await controller.load()
    const update = controller.update(true)
    await controller.update(false)
    expect(request).toHaveBeenCalledTimes(2)
    expect(controller.snapshot().busy).toBe(true)
    complete(configured)
    await update
    expect(controller.snapshot()).toMatchObject({ busy: false, settings: configured })
  })

  it('ignores an old directory response after cancellation and a fresh load', async () => {
    let complete!: (settings: DisplaySettings) => void
    const request = vi.fn().mockImplementationOnce(() => new Promise<DisplaySettings>(resolve => { complete = resolve })).mockResolvedValueOnce(configured)
    const controller = new DisplaySettingsController(request)
    const stale = controller.load()
    controller.cancel()
    await controller.load()
    complete(inherited)
    await stale
    expect(controller.snapshot()).toEqual({ settings: configured, busy: false, error: '', saved: false })
  })

  it('allows retry after a load failure and ignores a cancelled failure', async () => {
    let fail!: (error: Error) => void
    const request = vi.fn().mockRejectedValueOnce(new Error('Backend unavailable')).mockImplementationOnce(() => new Promise<DisplaySettings>((_resolve, reject) => { fail = reject })).mockResolvedValueOnce(inherited)
    const controller = new DisplaySettingsController(request)
    await controller.load()
    expect(controller.snapshot()).toMatchObject({ settings: null, busy: false, error: 'Backend unavailable' })
    const stale = controller.load()
    controller.cancel()
    await controller.load()
    fail(new Error('Old backend disconnected'))
    await stale
    expect(controller.snapshot()).toEqual({ settings: inherited, busy: false, error: '', saved: false })
  })
})
