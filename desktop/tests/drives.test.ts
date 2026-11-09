import { describe, expect, it } from 'vitest'
import { withDrives } from '../src/renderer/lib/vm-settings'
import type { DriveSettings, HardwareSettings } from '../src/shared/types'

describe('ordered drives', () => {
  const disks: DriveSettings[] = [{ id: 'drive-data', media: 'disk', path: null, format: 'qcow2', read_only: false, attached: true, interface: 'nvme', size_gib: 2 }, { id: 'drive-disc', media: 'cdrom', path: '/install.iso', format: 'raw', read_only: true, attached: true }]

  it('keeps an explicit optical boot selection through a reorder and clears it on deletion', () => {
    const settings = { drives: disks, boot_order: 'cdrom', boot_drive_id: 'drive-disc' } as HardwareSettings
    expect(withDrives(settings, [...disks].reverse()).boot_drive_id).toBe('drive-disc')
    expect(withDrives(settings, [disks[0]])).toMatchObject({ boot_order: 'auto', boot_drive_id: null })
  })
})
