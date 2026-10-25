import { describe, expect, it } from 'vitest'
import { driveLabel, moveDrive } from '../src/renderer/lib/drives'
import { withDrives } from '../src/renderer/lib/vm-settings'
import type { DriveSettings, HardwareSettings } from '../src/shared/types'

describe('ordered drives', () => {
  const disks: DriveSettings[] = [{ id: 'drive-data', media: 'disk', path: null, format: 'qcow2', read_only: false, attached: true, interface: 'nvme', size_gib: 2 }, { id: 'drive-disc', media: 'cdrom', path: '/install.iso', format: 'raw', read_only: true, attached: true }]
  it('moves entries without changing identity or unsaved settings', () => {
    const reordered = moveDrive(disks, disks[1].id, -1)
    expect(reordered).toEqual([disks[1], disks[0]])
    expect(disks[0].id).toBe('drive-data')
    expect(moveDrive(disks, disks[0].id, -1)).toBe(disks)
    expect(driveLabel(disks[0], disks)).toBe('NVMe 磁盘 1')
    expect(driveLabel(disks[1], disks)).toBe('USB 光驱 1')
  })
  it('keeps an explicit optical boot selection through a reorder and clears it on deletion', () => {
    const settings = { drives: disks, boot_order: 'cdrom', boot_drive_id: 'drive-disc' } as HardwareSettings
    expect(withDrives(settings, [...disks].reverse()).boot_drive_id).toBe('drive-disc')
    expect(withDrives(settings, [disks[0]])).toMatchObject({ boot_order: 'auto', boot_drive_id: null })
  })
})
