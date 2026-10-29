import type { HardwareSettings, Profile, VmDraft } from '../../shared/types'

export type SettingsSection = 'general' | 'resources' | 'disk' | 'devices' | 'display' | 'network' | 'audio'
export const sections: { id: SettingsSection; label: string }[] = [
  { id: 'general', label: '常规' }, { id: 'resources', label: '处理器与内存' },
  { id: 'disk', label: '启动' }, { id: 'devices', label: '设备与驱动器' }, { id: 'display', label: '显示' },
  { id: 'network', label: '网络' }, { id: 'audio', label: '声音' }
]
export const displayNames: Record<string, string> = { VGA: '标准 VGA', 'virtio-vga': 'VirtIO VGA', 'virtio-gpu-pci': 'VirtIO GPU', ramfb: 'RAMFB' }
export const networkNames = { 'restricted-lab': '隔离网络', offline: '离线', nat: 'NAT' }

export function profileSettings(profile: Profile, isoPath: string | null = null): HardwareSettings {
  return {
    display: { model: profile.devices.display_model, width: profile.display.width, height: profile.display.height, default_resolution: profile.devices.default_resolution ?? false },
    network: { model: (profile.network.model ?? null) as HardwareSettings['network']['model'], mac: profile.network.mac ?? null, forwards: [] },
    audio_enabled: profile.audio?.enabled ?? false, iso_path: null, boot_order: 'auto', boot_drive_id: null,
    drives: isoPath ? [{ id: `drive-${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`, media: 'cdrom', path: isoPath, format: 'raw', read_only: true, attached: true }] : []
  }
}

export function withDrives(settings: HardwareSettings, drives: NonNullable<HardwareSettings['drives']>): HardwareSettings {
  const optical = drives.filter(drive => drive.media === 'cdrom' && drive.path && drive.attached)
  return { ...settings, drives, boot_drive_id: optical.some(drive => drive.id === settings.boot_drive_id) ? settings.boot_drive_id : null, boot_order: settings.boot_order === 'cdrom' && !optical.length ? 'auto' : settings.boot_order }
}

export function validateVmSettings(draft: VmDraft, settings?: HardwareSettings | null): { section: SettingsSection; field: string; message: string } | null {
  if (!draft.name.trim()) return { section: 'general', field: 'edit-name', message: '请输入虚拟机名称。' }
  if (!Number.isInteger(draft.vcpus) || draft.vcpus < 1 || draft.vcpus > 64) return { section: 'resources', field: 'settings-cpu', message: 'CPU 核数必须为 1–64 的整数。' }
  if (!Number.isInteger(draft.memory_mib) || draft.memory_mib < 256 || draft.memory_mib > 1048576) return { section: 'resources', field: 'settings-memory', message: '内存必须为 256–1048576 MiB。' }
  if (!Number.isInteger(draft.disk_gib) || draft.disk_gib < 1 || draft.disk_gib > 4096) return { section: 'disk', field: 'disk', message: '磁盘容量必须为 1–4096 GiB 的整数。' }
  if (!settings) return null
  const drives = settings.drives ?? []
  if (drives.length > 9) return { section: 'devices', field: 'add-drive', message: '最多配置 9 个驱动器。' }
  for (const [index, drive] of drives.entries()) {
    if (drive.size_gib != null && (!Number.isInteger(drive.size_gib) || drive.size_gib < 1 || drive.size_gib > 4096)) return { section: 'devices', field: `drive-capacity-${drive.id}`, message: '空白磁盘容量须为 1–4096 GiB 的整数。' }
    if (drive.media === 'disk' && !drive.path && drive.size_gib == null) return { section: 'devices', field: `drive-path-${drive.id}`, message: `磁盘 ${index + 1} 需要选择镜像。` }
  }
  const { width, height, default_resolution: resolution } = settings.display
  if (resolution && (!Number.isInteger(width) || !Number.isInteger(height) || width < 2 || height < 2 || width > 8192 || height > 8192 || width * height > 16777216)) return { section: 'display', field: 'display-width', message: '分辨率须为 2–8192 的整数，且总像素不超过 16777216。' }
  const mac = settings.network.mac
  if (mac && (!/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i.test(mac) || (parseInt(mac.slice(0, 2), 16) & 1) || mac === '00:00:00:00:00:00')) return { section: 'network', field: 'network-mac', message: '请输入有效的非零单播 MAC 地址。' }
  const ports = new Set<string>()
  for (const [index, rule] of settings.network.forwards.entries()) {
    if (![rule.host_port, rule.guest_port].every(port => Number.isInteger(port) && port >= 1 && port <= 65535)) return { section: 'network', field: `host-port-${index}`, message: '端口必须为 1–65535 的整数。' }
    const key = `${rule.protocol}:${rule.host_port}`
    if (ports.has(key)) return { section: 'network', field: `host-port-${index}`, message: '同一协议不能重复使用宿主端口。' }
    ports.add(key)
  }
  const optical = drives.filter(drive => drive.media === 'cdrom' && drive.path && drive.attached)
  if (settings.boot_order === 'cdrom' && !optical.length) return { section: 'disk', field: 'boot-order', message: '光盘优先需要挂载 ISO。' }
  if (settings.boot_drive_id && !optical.some(drive => drive.id === settings.boot_drive_id)) return { section: 'disk', field: 'boot-drive', message: '请选择已连接介质的启动光驱。' }
  return null
}

export function sizeLabel(bytes: number | null): string {
  if (bytes == null) return '尚未分配'
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(2)} MiB`
}

export function errorSection(message: string): SettingsSection | null {
  if (/drives|附加|驱动器|设备 ID|读写磁盘/i.test(message)) return 'devices'
  if (/display|显卡|分辨率/i.test(message)) return 'display'
  if (/network|forward|mac|port|网卡|网络|端口/i.test(message)) return 'network'
  if (/iso|boot|光盘|介质/i.test(message)) return 'disk'
  if (/audio|声音|音频|声卡/i.test(message)) return 'audio'
  if (/vcpus|memory|cpu|内存/i.test(message)) return 'resources'
  if (/name|名称/i.test(message)) return 'general'
  return null
}
