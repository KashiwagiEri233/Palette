import { describe, expect, it } from 'vitest'
import { validateVmSettings } from '../src/renderer/lib/vm-settings'
import type { HardwareSettings, VmDraft } from '../src/shared/types'

const draft: VmDraft = { name: '开发环境', profile_id: 'default', source: 'image', path: '/disk.qcow2', image_format: 'qcow2', vcpus: 2, memory_mib: 4096, disk_gib: 24, network: 'nat' }
const hardware: HardwareSettings = { display: { model: 'VGA', width: 1280, height: 800, default_resolution: true }, network: { model: null, mac: null, forwards: [] }, audio_enabled: false, iso_path: null, boot_order: 'auto' }
describe('VM configuration validation', () => {
  it('rejects fractional CPU counts and oversized framebuffers', () => {
    expect(validateVmSettings({ ...draft, vcpus: 1.5 }, hardware)?.section).toBe('resources')
    expect(validateVmSettings(draft, { ...hardware, display: { ...hardware.display, width: 8192, height: 8192 } })?.section).toBe('display')
  })
  it('rejects multicast MACs and duplicate forwarding listeners', () => {
    expect(validateVmSettings(draft, { ...hardware, network: { ...hardware.network, mac: 'ff:ff:ff:ff:ff:ff' } })?.field).toBe('network-mac')
    const rule = { protocol: 'tcp' as const, host_port: 8080, guest_port: 80 }
    expect(validateVmSettings(draft, { ...hardware, network: { ...hardware.network, forwards: [rule, { ...rule, guest_port: 81 }] } })?.section).toBe('network')
  })
  it('requires mounted installation media for CD boot', () => {
    expect(validateVmSettings(draft, { ...hardware, boot_order: 'cdrom' })?.section).toBe('disk')
    expect(validateVmSettings(draft, { ...hardware, boot_order: 'cdrom', iso_path: '/install.iso' }, false)?.section).toBe('disk')
    expect(validateVmSettings(draft, { ...hardware, boot_order: 'cdrom', iso_path: '/install.iso' })).toBeNull()
  })
})
