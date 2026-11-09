import { describe, expect, it } from 'vitest'
import { configurationDraft, creationResourceError } from '../src/renderer/lib/os-presets'
import type { HardwareCapabilities, Profile, VmDraft } from '../src/shared/types'

const profile: Profile = { profile_id: 'host-windows', qemu: { arch: 'x86_64', accelerator: 'whpx' }, resources: { vcpus: 1, memory_mib: 512 }, images: { size_gib: 24 }, network: { mode: 'offline', model: 'e1000' }, devices: { display_model: 'VGA', default_resolution: true }, display: { width: 1280, height: 800 }, audio: { enabled: false } }
const previous: VmDraft = { name: '我的电脑', profile_id: '', source: 'iso', path: '/installer.iso', image_format: 'qcow2', vcpus: 1, memory_mib: 512, disk_gib: 24, network: 'offline' }
const caps: HardwareCapabilities = { available: true, reason: '', displays: [], network_models: [], audio_available: false, audio_backend: null, host: { vcpus: 4, memory_mib: 8192 } }

describe('OS preset configuration', () => {


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
