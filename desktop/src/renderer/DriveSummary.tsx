import type { DriveSettings } from '../shared/types'
import { driveLabel } from './VmSettingsNavigation'

export function DriveSummary({ drives }: { drives: DriveSettings[] }) {
  return <>{drives.map(drive => <div key={drive.id} className="col-span-2"><dt>{driveLabel(drive, drives)} · {drive.attached ? drive.media === 'cdrom' && !drive.path ? '空载' : '已连接' : drive.media === 'cdrom' ? '介质已弹出' : '已断开'}</dt><dd className="path-value">{drive.size_gib != null ? `${drive.size_gib} GiB · ${drive.format.toUpperCase()}` : drive.path ?? '空光驱'}</dd></div>)}</>
}
