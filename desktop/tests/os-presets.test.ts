import { describe, expect, it } from 'vitest'
import { configurationDraft, creationPayload, creationResourceError } from '../src/renderer/lib/os-presets'
import type { HardwareCapabilities, OsPreset, Profile, VmDraft } from '../src/shared/types'

const profile: Profile = { profile_id: 'host-windows', qemu: { arch: 'x86_64', accelerator: 'whpx' }, resources: { vcpus: 1, memory_mib: 512 }, images: { size_gib: 24 }, network: { mode: 'offline', model: 'e1000' }, devices: { display_model: 'VGA', default_resolution: true }, display: { width: 1280, height: 800 }, audio: { enabled: false } }
const previous: VmDraft = { name: '我的电脑', profile_id: '', source: 'iso', path: '/installer.iso', image_format: 'qcow2', vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }
const linux: OsPreset = { id: 'linux', name: 'Linux', vcpus: 2, memory_mib: 4096, disk_gib: 32, targets: [{ arch: 'x86_64', profile_id: profile.profile_id, accelerator: 'whpx', requires_emulation: false, available: true, reason: '', display_model: 'virtio-gpu-pci', network_model: 'virtio-net-pci' }] }
const caps: HardwareCapabilities = { available: true, reason: '', displays: [], network_models: [], audio_available: false, audio_backend: null, host: { vcpus: 4, memory_mib: 8192 } }

describe('OS preset configuration', () => {
  it('applies guest suggestions over the host profile while preserving the source and name', () => {
    const result = configurationDraft(profile, previous, linux, linux.targets[0])
    expect(result).toMatchObject({ os_preset: 'linux', profile_id: 'host-windows', name: previous.name, path: previous.path, source: 'iso', vcpus: 2, memory_mib: 4096, disk_gib: 32, network: 'offline' })
    expect(result.settings).toMatchObject({ display: { model: 'virtio-gpu-pci', default_resolution: false }, network: { model: 'virtio-net-pci' }, iso_path: previous.path, audio_enabled: false })
    expect(profile.devices.display_model).toBe('VGA')
  })
  it('sends user CPU, memory and disk changes without reapplying suggestions', () => {
    const result = configurationDraft(profile, previous, linux, linux.targets[0])
    const edited = { ...result, vcpus: 3, memory_mib: 6144, disk_gib: 72 }
    expect(creationPayload(edited, true)).toMatchObject({ os_preset: 'linux', vcpus: 3, memory_mib: 6144, disk_gib: 72, iso_attached: true })
  })
  it('custom keeps profile defaults and does not attach disk images as CD-ROMs', () => {
    const result = configurationDraft(profile, { ...previous, source: 'image', path: '/disk.raw' })
    expect(result).toMatchObject({ os_preset: 'custom', vcpus: 1, memory_mib: 512, disk_gib: 24 })
    expect(result.settings?.iso_path).toBeNull()
  })
  it('validates host resources and the imported image capacity independently of OS recommendations', () => {
    expect(creationResourceError({ ...previous, vcpus: 5 }, caps)).toContain('CPU')
    expect(creationResourceError({ ...previous, memory_mib: 8193 }, caps)).toContain('内存')
    expect(creationResourceError(previous, caps, 25)).toContain('镜像虚拟容量')
    expect(creationResourceError(previous, caps, 24)).toBeNull()
  })
})
