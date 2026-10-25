import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import type { DriveInterface, DriveSettings, HardwareCapabilities } from '../shared/types'
import { interfaceNames } from './lib/drives'

export function CreateDrive({ caps, onAdd, onClose }: { caps: HardwareCapabilities; onAdd: (drive: DriveSettings) => void; onClose: () => void }) {
  const [removable, setRemovable] = useState(false)
  const [capacity, setCapacity] = useState(32)
  const [format, setFormat] = useState<'raw' | 'qcow2'>('qcow2')
  const [selected, setSelected] = useState<DriveInterface>('virtio')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const media = removable ? 'cdrom' : 'disk'
  const interfaces = caps.drive_interfaces?.[media] ?? []
  const current = interfaces.includes(selected) ? selected : interfaces[0]
  async function add(importing: boolean) {
    if (!current || busy) return
    if (!removable && !importing && (!Number.isInteger(capacity) || capacity < 1 || capacity > 4096)) { setError('容量须为 1–4096 GiB 的整数。'); return }
    setBusy(true); setError('')
    try {
      const path = importing ? await window.palette.chooseFile(removable ? 'iso' : 'image') : null
      if (importing && !path) return
      onAdd({ id: `drive-${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`, media, interface: current, path, attached: true, read_only: removable, format: removable ? 'raw' : format, size_gib: removable || importing ? null : capacity, import_copy: !removable && importing })
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setBusy(false) }
  }
  return <Dialog open onOpenChange={value => { if (!value && !busy) onClose() }}><DialogContent scrollable closeDisabled={busy}><DialogHeader><DialogTitle>新建驱动器</DialogTitle><DialogDescription>配置接口后创建空白磁盘，或导入已有镜像。</DialogDescription></DialogHeader><fieldset disabled={busy} className="form-stack">
    <label className="create-emulation"><input type="checkbox" checked={removable} onChange={event => { setRemovable(event.target.checked); setSelected(event.target.checked ? 'usb' : 'virtio') }} />可移动介质</label>
    <p className="field-hint">{removable ? '创建光驱，介质文件保留在原位置；可选择 ISO 或保持空载。' : '磁盘文件保存在虚拟机目录内；导入会复制并转换镜像，保留源文件。'}</p>
    <div><Label htmlFor="new-drive-interface">接口</Label><FormSelect id="new-drive-interface" disabled={!interfaces.length} value={current ?? ''} onValueChange={value => setSelected(value as DriveInterface)} options={interfaces.map(value => ({ value, label: interfaceNames[value] }))} /></div>
    {!removable && <><div><Label htmlFor="new-drive-size">容量 / GiB</Label><Input id="new-drive-size" type="number" min={1} max={4096} value={capacity} onChange={event => setCapacity(Number(event.target.value))} /><p className="field-hint">创建空白磁盘时使用此容量；导入时使用源镜像容量。</p></div><div><Label htmlFor="new-drive-format">镜像格式</Label><FormSelect id="new-drive-format" value={format} onValueChange={value => setFormat(value as typeof format)} options={[{ value: 'qcow2', label: 'QCOW2' }, { value: 'raw', label: 'RAW' }]} /></div></>}
    {(!interfaces.length || error) && <p role="alert" className="text-destructive">{error || (!caps.drive_interfaces ? '后台未提供驱动器能力信息，请重启后台并重新连接。' : !caps.available ? caps.reason : '当前 QEMU 没有可用的接口。')}</p>}
  </fieldset><DialogFooter><Button variant="secondary" disabled={busy} onClick={onClose}>取消</Button><Button variant="outline" disabled={busy || !current} onClick={() => void add(true)}>{removable ? '浏览…' : '导入…'}</Button><Button disabled={busy || !current} onClick={() => void add(false)}>创建</Button></DialogFooter></DialogContent></Dialog>
}
