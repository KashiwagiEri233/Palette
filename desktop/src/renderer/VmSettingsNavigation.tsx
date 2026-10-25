import { Cpu, Disc3, HardDrive, Monitor, Network, Settings2, Volume2, Plus, Ellipsis } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu'
import type { DriveSettings } from '../shared/types'
import { sections, type SettingsSection } from './lib/vm-settings'
import { driveLabel } from './lib/drives'
export { driveLabel } from './lib/drives'

const icons = { general: Settings2, resources: Cpu, disk: HardDrive, display: Monitor, network: Network, audio: Volume2 }

export function VmSettingsNavigation({ section, driveId, drives, selectSection, selectDrive, createDrive, move, remove, canCreate, canEdit, summary }: {
  section: SettingsSection; driveId: string | null; drives: DriveSettings[];
  selectSection: (section: SettingsSection) => void; selectDrive: (id: string) => void;
  createDrive: () => void; move: (id: string, offset: -1 | 1) => void; remove: (id: string) => void; canCreate: boolean; canEdit: boolean;
  summary: { name: string; resources: string; disk: string }
}) {
  function sectionButton(id: keyof typeof icons) {
    const Icon = icons[id]
    return <Button key={id} variant="ghost" aria-pressed={section === id} onClick={() => selectSection(id)}><Icon size={17} aria-hidden="true" /><span>{sections.find(item => item.id === id)?.label}</span></Button>
  }
  return <nav className="vm-settings-navigation" aria-label="设置分类">
    <div className="settings-nav-group">{sectionButton('general')}{sectionButton('resources')}</div>
    <div className="settings-nav-group" role="group" aria-label="设备"><span className="settings-nav-heading">设备</span>{sectionButton('display')}{sectionButton('network')}{sectionButton('audio')}</div>
    <div className="settings-nav-group" role="group" aria-label="驱动器"><span className="settings-nav-heading">驱动器</span>{sectionButton('disk')}
      {drives.map((drive, index) => {
        const Icon = drive.media === 'disk' ? HardDrive : Disc3
        const label = driveLabel(drive, drives)
        const detail = !drive.attached ? drive.media === 'cdrom' ? '介质已弹出' : '已断开' : drive.size_gib != null ? `${drive.size_gib} GiB · ${drive.format.toUpperCase()}` : drive.path?.split(/[\\/]/).at(-1) ?? '空载'
        return <div className="settings-nav-row" key={drive.id}>
          <Button className="settings-nav-select" id={`settings-nav-${drive.id}`} data-drive-id={drive.id} variant="ghost" aria-label={label} aria-pressed={section === 'devices' && driveId === drive.id} title={drive.path ?? label} onClick={() => selectDrive(drive.id)}>
            <Icon size={17} aria-hidden="true" />
            <span className="settings-nav-drive"><span>{label}</span><small>{detail}</small></span>
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button className="settings-nav-actions" variant="ghost" size="icon" aria-label={`${label} 的操作`} disabled={!canEdit} />}><Ellipsis size={15} aria-hidden="true" /></DropdownMenuTrigger>
            <DropdownMenuContent side="right">
              <DropdownMenuItem disabled={index === 0} onClick={() => move(drive.id, -1)}>上移</DropdownMenuItem>
              <DropdownMenuItem disabled={index === drives.length - 1} onClick={() => move(drive.id, 1)}>下移</DropdownMenuItem>
              <DropdownMenuItem onClick={() => remove(drive.id)}>删除驱动器…</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      })}
      <Button id="add-drive" variant="ghost" aria-label="新建驱动器" disabled={!canCreate} onClick={createDrive}><Plus size={17} aria-hidden="true" /><span>新建…</span></Button>
      <span className="settings-nav-limit">驱动器 {drives.length} / 9</span>
    </div>
    <div className="vm-settings-summary"><strong>{summary.name}</strong><span>{summary.resources}</span><span>{summary.disk}</span></div>
  </nav>
}
