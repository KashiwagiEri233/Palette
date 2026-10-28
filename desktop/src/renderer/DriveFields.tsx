import { ErrorWithDebugInfo } from './components/CopyDebugInfoButton'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import type { DriveSettings, DriveInterface, HardwareCapabilities, ManagedDiskInfo } from '../shared/types'
import { driveInterface, interfaceNames } from './lib/drives'
import { sizeLabel } from './lib/vm-settings'

export function DriveFields({ drive, change, remove, resize, resizeDisabled, info, caps }: { drive: DriveSettings; change: (drive: DriveSettings) => void; remove: () => void; resize?: () => void; resizeDisabled?: boolean; info?: ManagedDiskInfo; caps: HardwareCapabilities }) {
  const [error, setError] = useState('')
  const kind = driveInterface(drive)
  const offered = caps.drive_interfaces?.[drive.media] ?? []
  const internal = !!info || drive.size_gib != null || drive.import_copy
  const allocated = info != null && info.allocated !== false
  const options = [...(!offered.includes(kind) ? [{ value: kind, label: `${interfaceNames[kind]}（当前配置）` }] : []), ...offered.map(value => ({ value, label: interfaceNames[value] }))]
  async function browse() {
    try { const path = await window.palette.chooseFile(drive.media === 'cdrom' ? 'iso' : 'image'); if (path) { change({ ...drive, path, attached: true, format: drive.media === 'cdrom' || /\.(raw|img)$/i.test(path) ? 'raw' : 'qcow2' }); setError('') } }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
  }
  return <div className="form-stack">
    <dl className="configuration-grid"><div><dt>介质类型</dt><dd>{drive.media === 'cdrom' ? '可移动光盘' : internal ? '虚拟机磁盘' : '外部磁盘'}</dd></div><div><dt>文件</dt><dd>{info?.source_name ?? info?.path?.split(/[\\/]/).at(-1) ?? drive.path?.split(/[\\/]/).at(-1) ?? '尚未分配'}</dd></div>{info && <><div><dt>虚拟容量</dt><dd>{sizeLabel(info.virtual_bytes ?? info.size_gib * 1024 ** 3)}</dd></div><div><dt>镜像格式</dt><dd>{(info.format ?? drive.format).toUpperCase()}</dd></div>{info.actual_bytes != null && <div><dt>宿主占用</dt><dd>{sizeLabel(info.actual_bytes)}</dd></div>}</>}</dl>
    <div><Label htmlFor={`drive-interface-${drive.id}`}>接口</Label><FormSelect id={`drive-interface-${drive.id}`} value={kind} options={options} onValueChange={value => change({ ...drive, interface: value as DriveInterface })} /><p className="field-hint">客户机需要支持此存储接口。更改后在下次启动生效。</p></div>
    {!internal && <div><Label htmlFor={`drive-path-${drive.id}`}>介质文件</Label><div className="file-field"><Input id={`drive-path-${drive.id}`} readOnly value={drive.path ?? ''} placeholder="空载" /><Button variant="outline" onClick={() => void browse()}>浏览…</Button></div></div>}
    {!allocated && drive.size_gib != null && <div><Label htmlFor={`drive-capacity-${drive.id}`}>容量 / GiB</Label><Input id={`drive-capacity-${drive.id}`} type="number" min={1} max={4096} value={drive.size_gib} onChange={event => change({ ...drive, size_gib: Number(event.target.value) })} /></div>}
    {drive.media === 'disk' && !info && <><div><Label htmlFor={`drive-format-${drive.id}`}>镜像格式</Label><FormSelect id={`drive-format-${drive.id}`} value={drive.format} onValueChange={format => change({ ...drive, format: format as 'raw' | 'qcow2' })} options={[{ value: 'qcow2', label: 'QCOW2' }, { value: 'raw', label: 'RAW' }]} /></div><p className="field-hint">{drive.import_copy ? '保存时复制并转换源镜像，源文件保留。' : internal ? '保存时创建磁盘文件，客户机内需初始化和格式化。' : '这是外部镜像；选择读写会直接修改原文件。'}</p></>}
    <div className="resource-fields"><div><Label htmlFor={`drive-state-${drive.id}`}>连接状态</Label><FormSelect id={`drive-state-${drive.id}`} value={drive.attached ? 'attached' : 'ejected'} onValueChange={value => change({ ...drive, attached: value === 'attached' })} options={[{ value: 'attached', label: drive.media === 'cdrom' && !drive.path ? '空载光驱' : '连接介质' }, { value: 'ejected', label: drive.media === 'cdrom' ? '弹出介质' : '断开磁盘' }]} /></div>{drive.media === 'disk' && <div><Label htmlFor={`drive-access-${drive.id}`}>访问方式</Label><FormSelect id={`drive-access-${drive.id}`} value={drive.read_only ? 'ro' : 'rw'} onValueChange={value => change({ ...drive, read_only: value === 'ro' })} options={[{ value: 'ro', label: '只读' }, { value: 'rw', label: '读写' }]} /></div>}</div>
    {info?.path && <p className="path-value">{info.path}</p>}
    {resize && <div><Button variant="outline" disabled={resizeDisabled} onClick={resize}>扩容磁盘</Button><p className="field-hint">容量只能增大；需先保存或丢弃其他修改。</p></div>}
    <div className="drive-remove"><Button variant="destructive" onClick={remove}>删除驱动器…</Button><p className="field-hint">{info ? '保存后删除此磁盘文件及设备，取消设置可放弃删除。' : '删除在保存后生效；外部源文件和 ISO 保留。'}</p></div>
    {error && <ErrorWithDebugInfo className="text-destructive" error={error} />}
  </div>
}
