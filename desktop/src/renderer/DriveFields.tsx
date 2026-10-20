import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import { FolderIcon, PlusIcon } from '@/components/icons'
import type { DriveSettings, HardwareSettings } from '../shared/types'

export function DriveFields({ settings, change }: { settings: HardwareSettings; change: (settings: HardwareSettings) => void }) {
  const [error, setError] = useState('')
  const drives = settings.drives ?? []
  function update(id: string, patch: Partial<DriveSettings>) {
    change({ ...settings, drives: drives.map(drive => drive.id === id ? { ...drive, ...patch } : drive) })
  }
  function add(media: DriveSettings['media'], path: string | null = null, format: DriveSettings['format'] = 'raw') {
    change({ ...settings, drives: [...drives, { id: `drive-${crypto.randomUUID().replaceAll('-', '').slice(0, 12)}`, media, path, format, read_only: true, attached: true }] })
  }
  async function choose(drive?: DriveSettings) {
    try {
      const media = drive?.media ?? 'disk'
      const path = await window.palette.chooseFile(media === 'cdrom' ? 'iso' : 'image')
      if (!path) return
      const format = media === 'cdrom' || /\.(raw|img)$/i.test(path) ? 'raw' : 'qcow2'
      if (drive) update(drive.id, { path, format, attached: true })
      else add('disk', path, format)
      setError('')
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
  }
  return <div className="form-stack">
    <div className="drive-add-actions"><Button id="add-drive" variant="outline" disabled={drives.length >= 8} onClick={() => void choose()}><PlusIcon className="size-4" />添加磁盘</Button><Button variant="outline" disabled={drives.length >= 8} onClick={() => add('cdrom')}><PlusIcon className="size-4" />添加光驱</Button></div>
    <p className="field-hint">最多添加 8 个附加驱动器。磁盘接入已有镜像，光驱可空载或装载 ISO。系统磁盘和安装光盘在“磁盘与启动”中管理。</p>
    {!drives.length && <p className="field-hint">尚未添加附加驱动器。</p>}
    {drives.map((drive, index) => <section key={drive.id} className="drive-card" aria-label={`附加${drive.media === 'disk' ? '磁盘' : '光驱'} ${index + 1}`}>
      <div className="drive-card-heading"><strong>{drive.media === 'disk' ? '磁盘' : '光驱'} {index + 1}</strong><Button variant="ghost" size="sm" aria-label={`移除驱动器 ${index + 1}`} onClick={() => change({ ...settings, drives: drives.filter(item => item.id !== drive.id) })}>移除</Button></div>
      <div><Label htmlFor={`drive-path-${drive.id}`}>介质文件</Label><div className="file-field"><Input id={`drive-path-${drive.id}`} readOnly value={drive.path ?? ''} placeholder={drive.media === 'cdrom' ? '空光驱' : '选择磁盘镜像'} /><Button variant="secondary" onClick={() => void choose(drive)}><FolderIcon className="size-4" />选择{drive.media === 'cdrom' ? ' ISO' : '镜像'}</Button></div></div>
      <div className="resource-fields"><div><Label htmlFor={`drive-state-${drive.id}`}>连接状态</Label><FormSelect id={`drive-state-${drive.id}`} value={drive.attached ? 'attached' : 'ejected'} onValueChange={value => update(drive.id, { attached: value === 'attached' })} options={[{ value: 'attached', label: drive.media === 'cdrom' && !drive.path ? '空载光驱' : '连接介质' }, { value: 'ejected', label: drive.media === 'cdrom' ? '弹出介质' : '断开磁盘' }]} /></div>
      {drive.media === 'disk' ? <div><Label htmlFor={`drive-access-${drive.id}`}>访问方式</Label><FormSelect id={`drive-access-${drive.id}`} value={drive.read_only ? 'ro' : 'rw'} onValueChange={value => update(drive.id, { read_only: value === 'ro' })} options={[{ value: 'ro', label: '只读' }, { value: 'rw', label: '读写' }]} /></div> : <div><Label>访问方式</Label><p className="field-hint">只读 · USB 光驱</p></div>}</div>
      {drive.media === 'disk' && <div><Label htmlFor={`drive-format-${drive.id}`}>镜像格式</Label><FormSelect id={`drive-format-${drive.id}`} value={drive.format} onValueChange={value => update(drive.id, { format: value as DriveSettings['format'] })} options={[{ value: 'qcow2', label: 'QCOW2' }, { value: 'raw', label: 'RAW' }]} /></div>}
      {drive.media === 'disk' && <p className="field-hint">VirtIO 磁盘需要客户机对应驱动。{drive.read_only ? '镜像文件保持只读。' : '读写模式会直接修改所选镜像。'}</p>}
    </section>)}
    <p className="field-hint">移除只断开设备，不删除镜像文件。附加驱动器不改变系统启动顺序。</p>
    {error && <p role="alert" className="text-destructive">{error}</p>}
  </div>
}
