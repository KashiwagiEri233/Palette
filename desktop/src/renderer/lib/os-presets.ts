import type { HardwareCapabilities, OsPreset, OsPresetTarget, Profile, VmDraft } from '../../shared/types'
import { profileSettings, withDrives } from './vm-settings'
import type { HardwareSettings } from '../../shared/types'

export const osNames = { windows: 'Windows', linux: 'Linux', custom: '自定义' }
export const archName = (arch: string): string => arch === 'aarch64' ? 'ARM64' : arch

/** Called only for an explicit configuration selection, never by polling effects. */
export function configurationDraft(profile: Profile, previous: VmDraft, preset?: OsPreset, target?: OsPresetTarget): VmDraft {
  const settings = profileSettings(profile, previous.source === 'iso' ? previous.path || null : null)
  if (settings && target) {
    settings.display.model = target.display_model
    settings.display.default_resolution = false
    settings.network.model = target.network_model
  }
  return {
    ...previous, os_preset: preset?.id ?? 'custom', profile_id: profile.profile_id,
    vcpus: preset?.vcpus ?? profile.resources.vcpus,
    memory_mib: preset?.memory_mib ?? profile.resources.memory_mib,
    disk_gib: preset?.disk_gib ?? profile.images.size_gib,
    network: ['offline', 'nat', 'restricted-lab'].includes(profile.network.mode) ? profile.network.mode as VmDraft['network'] : 'restricted-lab',
    settings
  }
}

export function creationResourceError(draft: VmDraft, caps: HardwareCapabilities | null, minimumDisk = 0): string | null {
  if (caps?.host.vcpus && draft.vcpus > caps.host.vcpus) return `CPU 核数不能超过宿主的 ${caps.host.vcpus} 核。`
  if (caps?.host.memory_mib && draft.memory_mib > caps.host.memory_mib) return '内存大小不能超过宿主总内存。'
  if (draft.disk_gib < minimumDisk) return `磁盘容量不能小于镜像虚拟容量（至少 ${minimumDisk} GiB）。`
  return null
}

export function sourceOptical(settings: HardwareSettings, path: string | null, oldPath: string): HardwareSettings {
  const drives = settings.drives ?? []
  const existing = drives.find(drive => drive.media === 'cdrom' && drive.path === oldPath)
  const remaining = drives.filter(drive => drive.id !== existing?.id)
  if (!path) return withDrives(settings, remaining)
  const drive = existing ? { ...existing, path, attached: true } : { id: `drive-${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`, media: 'cdrom' as const, path, format: 'raw' as const, read_only: true, attached: true }
  return withDrives(settings, existing ? drives.map(item => item.id === existing.id ? drive : item) : [drive, ...drives])
}

export function creationPayload(draft: VmDraft): Record<string, unknown> {
  return { ...draft, os_preset: draft.os_preset ?? 'custom', iso_attached: false }
}
