import { describe, expect, it } from 'vitest'
import { validateVmSettings, withDrives } from '../src/renderer/lib/vm-settings'
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
  it('allows any connected optical drive for CD boot', () => {
    const cd = { id: 'drive-disc', media: 'cdrom' as const, path: '/disc.iso', format: 'raw' as const, read_only: true, attached: true }
    expect(validateVmSettings(draft, { ...hardware, boot_order: 'cdrom' })?.section).toBe('disk')
    expect(validateVmSettings(draft, { ...hardware, boot_order: 'cdrom', drives: [{ ...cd, attached: false }] })?.section).toBe('disk')
    expect(validateVmSettings(draft, { ...hardware, boot_order: 'cdrom', drives: [cd], boot_drive_id: cd.id })).toBeNull()
    expect(withDrives({ ...hardware, boot_order: 'cdrom', boot_drive_id: cd.id }, [])).toMatchObject({ boot_order: 'auto', boot_drive_id: null })
  })
  it('validates blank disk capacity without requiring an external file', () => {
    const disk = { id: 'drive-data', media: 'disk' as const, path: null, format: 'qcow2' as const, read_only: false, attached: true, size_gib: 32 }
    expect(validateVmSettings(draft, { ...hardware, drives: [disk] })).toBeNull()
    for (const size of [0, 1.5, 4097]) expect(validateVmSettings(draft, { ...hardware, drives: [{ ...disk, size_gib: size }] })?.field).toBe('drive-capacity-drive-data')
  })
})
