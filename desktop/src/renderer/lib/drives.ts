import type { DriveInterface, DriveSettings } from '../../shared/types'

export const interfaceNames: Record<DriveInterface, string> = { virtio: 'VirtIO', ide: 'IDE', scsi: 'SCSI', nvme: 'NVMe', usb: 'USB' }
export const driveInterface = (drive: DriveSettings): DriveInterface => drive.interface ?? (drive.media === 'cdrom' ? 'usb' : 'virtio')
export function driveLabel(drive: DriveSettings, drives: DriveSettings[]): string {
  const kind = driveInterface(drive)
  const index = drives.filter(item => item.media === drive.media && driveInterface(item) === kind).findIndex(item => item.id === drive.id) + 1
  return `${interfaceNames[kind]} ${drive.media === 'cdrom' ? '光驱' : '磁盘'} ${index}`
}
export function moveDrive(drives: DriveSettings[], id: string, offset: -1 | 1): DriveSettings[] {
  const index = drives.findIndex(item => item.id === id)
  const next = index + offset
  if (index < 0 || next < 0 || next >= drives.length) return drives
  const result = [...drives]
  ;[result[index], result[next]] = [result[next], result[index]]
  return result
}
