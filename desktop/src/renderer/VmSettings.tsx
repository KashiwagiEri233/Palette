import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { PlusIcon } from '@/components/icons'
import { CreateDrive } from './CreateDrive'
import { moveDrive } from './lib/drives'
import { DriveFields } from './DriveFields'
import { VmSettingsNavigation, driveLabel } from './VmSettingsNavigation'
import type { DriveSettings, HardwareCapabilities, HardwareSettings, ManagedDiskInfo, OwnerInfo, VmDraft, VmRecord, VmSettingsResult } from '../shared/types'
import { displayNames, networkNames, sections, sizeLabel, validateVmSettings, errorSection, withDrives, type SettingsSection } from './lib/vm-settings'

const errorText = (error: unknown): string => String(error instanceof Error ? error.message : error).replace(/^Error invoking remote method '[^']+': Error: /, '')
const resolutions = ['1024x768', '1280x800', '1366x768', '1920x1080', '2560x1440', '3840x2160']

export function ResourceInputs({ draft, change }: { draft: VmDraft; change: (key: 'vcpus' | 'memory_mib', value: number) => void }) {
  return <div className="resource-fields"><div><Label htmlFor="settings-cpu">CPU 核数</Label><Input id="settings-cpu" type="number" min={1} max={64} value={draft.vcpus} onChange={event => change('vcpus', Number(event.target.value))} /></div><div><Label htmlFor="settings-memory">内存 / GiB</Label><Input id="settings-memory" type="number" min={0.25} max={1024} step={0.25} value={draft.memory_mib / 1024} onChange={event => change('memory_mib', Number(event.target.value) * 1024)} /></div></div>
}

export function HardwareFields({ section, settings, change, caps, draft, setDraft, includeMode = true, running = false, disabled = false }: {
  section: SettingsSection; settings: HardwareSettings; change: (settings: HardwareSettings) => void; caps: HardwareCapabilities | null;
  draft: VmDraft; setDraft: (draft: VmDraft) => void; includeMode?: boolean; running?: boolean; disabled?: boolean
}) {
  const optical = (settings.drives ?? []).filter(drive => drive.media === 'cdrom')
  const bootable = optical.filter(drive => drive.attached && drive.path)
  const display = settings.display
  const resolutionSupported = caps?.displays.find(item => item.model === display.model)?.resolution ?? false
  const network = settings.network
  const [customResolution, setCustomResolution] = useState(false)
  return <div className="form-stack">
    {section === 'disk' && <>
      <div><Label htmlFor="boot-order">启动顺序</Label><FormSelect id="boot-order" disabled={disabled} value={settings.boot_order} onValueChange={value => change({ ...settings, boot_order: value as HardwareSettings['boot_order'] })} options={[{ value: 'auto', label: '自动' }, { value: 'disk', label: '硬盘优先' }, { value: 'cdrom', label: '光盘优先', disabled: !bootable.length }]} /><p className="field-hint">自动模式首次启动时优先可用光驱，后续优先硬盘；同类介质按侧栏顺序启动。已保存的 UEFI 启动项仍可能影响系统选择。</p></div>
      <div><Label htmlFor="boot-drive">启动光驱</Label><FormSelect id="boot-drive" disabled={disabled || !bootable.length} value={settings.boot_drive_id ?? ''} onValueChange={value => change({ ...settings, boot_drive_id: value || null })} options={[{ value: '', label: '首个可用光驱' }, ...optical.map(drive => ({ value: drive.id, label: driveLabel(drive, settings.drives ?? []), disabled: !drive.attached || !drive.path }))]} /><p className="field-hint">在侧栏选择光驱可装载、更换或弹出介质。</p></div>
    </>}
    {section === 'display' && <>
      <div><Label htmlFor="display-model">虚拟显卡</Label><FormSelect id="display-model" disabled={disabled} value={display.model} onValueChange={model => change({ ...settings, display: { ...display, model, default_resolution: caps?.displays.find(item => item.model === model)?.resolution ?? false } })} options={[...(!caps?.displays.some(item => item.model === display.model) ? [{ value: display.model, label: `${displayNames[display.model] ?? display.model}（当前配置）` }] : []), ...(caps?.displays.map(item => ({ value: item.model, label: displayNames[item.model] ?? item.model })) ?? [])]} /><p className="field-hint">更换显卡可能需要客户机安装对应驱动。</p></div>
      <div><Label htmlFor="resolution-mode">默认分辨率</Label><FormSelect id="resolution-mode" disabled={disabled || !resolutionSupported} value={display.default_resolution ? 'custom' : 'system'} onValueChange={value => change({ ...settings, display: { ...display, default_resolution: value === 'custom' } })} options={[{ value: 'system', label: '由固件或系统决定' }, { value: 'custom', label: '指定启动分辨率' }]} /></div>
      {resolutionSupported && display.default_resolution && <><div><Label htmlFor="resolution-preset">分辨率预设</Label><FormSelect id="resolution-preset" disabled={disabled} value={!customResolution && resolutions.includes(`${display.width}x${display.height}`) ? `${display.width}x${display.height}` : 'custom'} onValueChange={value => { setCustomResolution(value === 'custom'); if (value !== 'custom') { const [width, height] = value.split('x').map(Number); change({ ...settings, display: { ...display, width, height } }) } }} options={[{ value: 'custom', label: '自定义' }, ...resolutions.map(value => ({ value, label: value.replace('x', ' × ') }))]} /></div><div className="resource-fields"><div><Label htmlFor="display-width">宽度 / px</Label><Input id="display-width" type="number" min={2} max={8192} value={display.width} onChange={event => change({ ...settings, display: { ...display, width: Number(event.target.value) } })} /></div><div><Label htmlFor="display-height">高度 / px</Label><Input id="display-height" type="number" min={2} max={8192} value={display.height} onChange={event => change({ ...settings, display: { ...display, height: Number(event.target.value) } })} /></div></div></>}
      <p className="field-hint">{resolutionSupported ? '这是启动时的显示提示，实际分辨率由客户机决定。控制台缩放不改变客户机分辨率。' : '当前显卡不支持指定启动分辨率，由固件或系统决定。'}</p>
    </>}
    {section === 'network' && <>
      {includeMode && <div><Label htmlFor="settings-network-mode">网络模式</Label><FormSelect id="settings-network-mode" disabled={disabled} value={draft.network} onValueChange={value => setDraft({ ...draft, network: value as VmDraft['network'] })} options={Object.entries(networkNames).map(([value, label]) => ({ value, label }))} /></div>}
      <p className="field-hint">{draft.network === 'offline' ? '不连接网卡，保留的网卡设置在启用网络后生效。' : draft.network === 'nat' ? '客户机通过宿主访问外部网络。' : '限制客户机外部访问；显式端口转发仍可连接客户机服务。'}</p>
      <div><Label htmlFor="network-model">虚拟网卡</Label><FormSelect id="network-model" disabled={disabled} value={network.model ?? ''} onValueChange={value => change({ ...settings, network: { ...network, model: (value || null) as HardwareSettings['network']['model'] } })} options={[{ value: '', label: '沿用默认' }, ...(network.model && !caps?.network_models.includes(network.model) ? [{ value: network.model, label: `${network.model}（当前配置）` }] : []), ...(caps?.network_models.map(value => ({ value, label: value === 'virtio-net-pci' ? 'VirtIO 网络' : value.toUpperCase() })) ?? [])]} /></div>
      <div><Label htmlFor="network-mac">MAC 地址</Label><Input id="network-mac" value={network.mac ?? ''} placeholder="自动分配" onChange={event => change({ ...settings, network: { ...network, mac: event.target.value || null } })} /></div>
      <div className="settings-forward-heading"><div><Label>端口转发</Label><p className="field-hint">宿主监听地址固定为 127.0.0.1。</p></div><Button variant="outline" size="sm" disabled={network.forwards.length >= 32} onClick={() => change({ ...settings, network: { ...network, forwards: [...network.forwards, { protocol: 'tcp', host_port: 8080, guest_port: 80 }] } })}><PlusIcon className="size-4" />添加</Button></div>
      {network.forwards.length ? network.forwards.map((rule, index) => <div className="forward-row" key={index}><div><Label htmlFor={`forward-protocol-${index}`}>协议</Label><FormSelect id={`forward-protocol-${index}`} disabled={disabled} value={rule.protocol} onValueChange={value => change({ ...settings, network: { ...network, forwards: network.forwards.map((item, i) => i === index ? { ...item, protocol: value as 'tcp' | 'udp' } : item) } })} options={[{ value: 'tcp', label: 'TCP' }, { value: 'udp', label: 'UDP' }]} /></div><div><Label htmlFor={`host-port-${index}`}>宿主端口</Label><Input id={`host-port-${index}`} type="number" min={1} max={65535} value={rule.host_port} onChange={event => change({ ...settings, network: { ...network, forwards: network.forwards.map((item, i) => i === index ? { ...item, host_port: Number(event.target.value) } : item) } })} /></div><div><Label htmlFor={`guest-port-${index}`}>客户机端口</Label><Input id={`guest-port-${index}`} type="number" min={1} max={65535} value={rule.guest_port} onChange={event => change({ ...settings, network: { ...network, forwards: network.forwards.map((item, i) => i === index ? { ...item, guest_port: Number(event.target.value) } : item) } })} /></div><Button variant="ghost" aria-label={`移除转发 ${index + 1}`} onClick={() => change({ ...settings, network: { ...network, forwards: network.forwards.filter((_, i) => i !== index) } })}>移除</Button></div>) : <p className="field-hint">尚未添加端口转发。</p>}
    </>}
    {section === 'audio' && <>
      <div><Label htmlFor="audio-enabled">声音输出</Label><FormSelect id="audio-enabled" disabled={disabled} value={settings.audio_enabled ? 'enabled' : 'disabled'} onValueChange={value => change({ ...settings, audio_enabled: value === 'enabled' })} options={[{ value: 'disabled', label: '关闭' }, { value: 'enabled', label: '启用声音', disabled: !caps?.audio_available }]} /></div>
      <dl className="configuration-grid"><div><dt>虚拟声卡</dt><dd>Intel HDA</dd></div><div><dt>输出设备</dt><dd>宿主默认音频设备</dd></div><div><dt>输出状态</dt><dd>{caps?.audio_available ? settings.audio_enabled ? running ? '已启用' : '下次启动时启用' : '已关闭' : '当前环境不支持声音输出'}</dd></div></dl>
      <p className="field-hint">仅播放客户机声音，不采集麦克风。客户机需要声卡驱动，宿主音量由系统控制。</p>
    </>}
    {caps && !caps.available && <p role="status" className="field-hint">{caps.reason}</p>}
  </div>
}

export function VmSettings({ vm, owner, onClose, onSaved }: { vm: VmRecord; owner: OwnerInfo | null; onClose: () => void; onSaved: () => Promise<void> }) {
  const supported = !!owner?.features.includes('vm-settings-v2') && owner.features.includes('vm-drives-v1')
  const [result, setResult] = useState<VmSettingsResult | null>(null)
  const [draft, setDraft] = useState<VmDraft | null>(null)
  const [settings, setSettings] = useState<HardwareSettings | null>(null)
  const [section, setSection] = useState<SettingsSection>('general')
  const [driveId, setDriveId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [removing, setRemoving] = useState<{ id: string; label: string } | null>(null)
  const [deletedIds, setDeletedIds] = useState<string[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [leave, setLeave] = useState(false)
  const [resize, setResize] = useState<string | null>(null)
  const [capacity, setCapacity] = useState(0)
  const [notice, setNotice] = useState('')
  const body = useRef<HTMLDivElement>(null)
  const dirty = !!result && (deletedIds.length > 0 || JSON.stringify({ draft, settings }) !== JSON.stringify({ draft: result.draft, settings: result.settings }))
  const editable = !!result?.editable && ['STOPPED', 'FAILED'].includes(vm.status.state)
  const drives = settings?.drives ?? []
  const activeDrive = drives.find(drive => drive.id === driveId) ?? (section === 'devices' ? drives[0] : undefined)
  const retained = Object.entries(result?.managed_disks ?? {}).filter(([id]) => !drives.some(drive => drive.id === id) && !deletedIds.includes(id))
  const resizeInfo = resize ? result?.managed_disks?.[resize] : undefined
  const resizeMinimum = resizeInfo ? Math.max(resizeInfo.size_gib, (resizeInfo.virtual_bytes ?? 0) / 1024 ** 3) : 0
  useEffect(() => { body.current?.scrollTo(0, 0) }, [section, driveId])
  function selectSection(value: SettingsSection) { setSection(value); setDriveId(null); setError('') }
  function selectDrive(id: string) { setSection('devices'); setDriveId(id); setError('') }
  function apply(data: VmSettingsResult) { setResult(data); setDraft(data.draft); setSettings(data.settings); setDeletedIds([]) }
  useEffect(() => { let live = true; if (supported) void window.palette.request('vms.settings.get', vm.vm_id).then(data => { if (live) apply(data as VmSettingsResult) }).catch(reason => { if (live) setError(errorText(reason)) }); return () => { live = false } }, [vm.vm_id, supported])
  function close() { if (!saving && !creating) { if (dirty) setLeave(true); else onClose() } }
  function addDrive(drive: DriveSettings) { if (editable && drives.length < 9) { setSettings(previous => previous ? withDrives(previous, [...(previous.drives ?? []), drive]) : previous); setCreating(false); selectDrive(drive.id) } }
  function reorder(id: string, offset: -1 | 1) { if (editable && !saving) { setSettings(previous => previous ? withDrives(previous, moveDrive(previous.drives ?? [], id, offset)) : previous); selectDrive(id) } }
  function requestRemove(id: string) { if (editable && !saving) { const drive = drives.find(item => item.id === id); setRemoving({ id, label: drive ? driveLabel(drive, drives) : id }) } }
  function remove() {
    if (!removing || !editable || saving) return
    const index = drives.findIndex(drive => drive.id === removing.id)
    const remaining = drives.filter(drive => drive.id !== removing.id)
    if (result?.managed_disks?.[removing.id]) setDeletedIds(previous => [...new Set([...previous, removing.id])])
    setSettings(previous => previous ? withDrives(previous, remaining) : previous)
    setRemoving(null)
    const next = remaining[Math.min(Math.max(index, 0), remaining.length - 1)]
    if (next) selectDrive(next.id); else selectSection('disk')
  }
  async function save(exit = false) {
    if (!editable || !result || !draft || !settings || saving) return
    const invalid = validateVmSettings(draft, settings)
    if (invalid) { setSection(invalid.section); if (invalid.section === 'devices') setDriveId(drives.find(drive => invalid.field.includes(drive.id))?.id ?? drives[0]?.id ?? null); setError(invalid.message); setLeave(false); return }
    setSaving(true); setError('')
    try {
      const reply = await window.palette.request('vms.update', vm.vm_id, { revision: result.revision, name: draft.name.trim(), vcpus: draft.vcpus, memory_mib: draft.memory_mib, network: draft.network, settings, iso_attached: false, delete_drives: deletedIds }) as { pending_disk_deletions?: string[] }
      apply(await window.palette.request('vms.settings.get', vm.vm_id) as VmSettingsResult)
      setLeave(false); await onSaved()
      if (reply.pending_disk_deletions?.length) { selectSection('disk'); setError('配置已保存，部分磁盘文件清理失败。请在未使用的磁盘中重试删除并保存。') }
      else if (exit) onClose()
    } catch (reason) {
      const message = errorText(reason); setLeave(false); setError(message)
      try { const latest = await window.palette.request('vms.settings.get', vm.vm_id) as VmSettingsResult; if (latest.revision !== result.revision) apply(latest) } catch { /* Retain edits if the backend is unavailable. */ }
      const target = errorSection(message); if (target) setSection(target)
    } finally { setSaving(false) }
  }
  async function expand() {
    if (!editable || !result || !resize) return
    setSaving(true); setError('')
    try { await window.palette.request('vms.disk.resize', vm.vm_id, { revision: result.revision, drive_id: resize, disk_gib: capacity }); apply(await window.palette.request('vms.settings.get', vm.vm_id) as VmSettingsResult); selectDrive(resize); setResize(null); setNotice('磁盘已扩容。请在客户机中扩展分区。'); await onSaved() }
    catch (reason) { setError(errorText(reason)); setResize(null) }
    finally { setSaving(false) }
  }
  return <>
    <Dialog open onOpenChange={value => { if (!value) close() }}><DialogContent className="vm-settings-dialog"><DialogHeader><DialogTitle>虚拟机设置</DialogTitle><DialogDescription>{vm.name} · {editable ? '更改会在保存后生效' : '关机后可修改硬件配置'}</DialogDescription></DialogHeader>
      {!supported ? <p role="status">当前后台需要升级：请重启后台并重新连接，以加载驱动器管理功能。</p> : !result || !draft || !settings ? <p role="status">{error || '正在读取配置…'}</p> : <div className="vm-settings-layout">
        <VmSettingsNavigation section={section} driveId={activeDrive?.id ?? null} drives={drives} selectSection={selectSection} selectDrive={selectDrive} createDrive={() => setCreating(true)} move={reorder} remove={requestRemove} canEdit={editable && !saving} canCreate={editable && !saving && drives.length < 9} summary={{ name: draft.name || vm.name, resources: `${draft.vcpus} 核 · ${draft.memory_mib / 1024} GiB`, disk: `${drives.filter(drive => drive.media === 'disk').length} 块磁盘 · ${networkNames[draft.network]}` }} />
        <div ref={body} className="vm-settings-body"><h2>{section === 'devices' && activeDrive ? driveLabel(activeDrive, drives) : sections.find(item => item.id === section)?.label}</h2>{notice && <p role="status" className="settings-notice">{notice}</p>}
          <fieldset disabled={!editable || saving} className="settings-fieldset">
            {section === 'general' && <div className="form-stack"><div><Label htmlFor="edit-name">名称</Label><Input id="edit-name" maxLength={120} value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} /></div><dl className="configuration-grid"><div><dt>系统配置</dt><dd>{draft.profile_id}</dd></div><div><dt>架构</dt><dd>{vm.status.guest_arch ?? '沿用原配置'}</dd></div></dl><div><Label>原始来源</Label><p className="path-value">{draft.path}</p></div></div>}
            {section === 'resources' && <div className="form-stack"><ResourceInputs draft={draft} change={(key, value) => setDraft({ ...draft, [key]: value })} /><p className="field-hint">宿主资源：{result.capabilities.host.vcpus ?? '—'} 个逻辑核心 / {sizeLabel(result.capabilities.host.memory_mib * 1024 ** 2)} 内存。</p></div>}
            {section === 'disk' && retained.length > 0 && <div className="settings-disk-section"><h3>未使用的磁盘</h3>{retained.map(([id, info]) => <div className="settings-preserved" key={id} data-retained-drive-id={id}><Label>{id} · {info.size_gib} GiB{info.deleting ? ' · 待完成删除' : ''}</Label><p className="path-value">{info.path}</p><Button variant="destructive" onClick={() => requestRemove(id)}>删除驱动器…</Button></div>)}</div>}
            {section === 'devices' && activeDrive && <DriveFields key={activeDrive.id} drive={activeDrive} info={result.managed_disks?.[activeDrive.id]} caps={result.capabilities} change={drive => { setError(''); setSettings(previous => previous ? withDrives(previous, (previous.drives ?? []).map(item => item.id === drive.id ? drive : item)) : previous) }} remove={() => requestRemove(activeDrive.id)} resize={result.managed_disks?.[activeDrive.id] && !result.managed_disks[activeDrive.id].deleting ? () => { const info = result.managed_disks![activeDrive.id]; setCapacity(Math.ceil(Math.max(info.size_gib, (info.virtual_bytes ?? 0) / 1024 ** 3)) + 1); setResize(activeDrive.id) } : undefined} resizeDisabled={dirty || !owner?.features.includes('disk-resize')} />}
            {['disk', 'display', 'network', 'audio'].includes(section) && <HardwareFields section={section} settings={settings} change={setSettings} caps={result.capabilities} draft={draft} setDraft={setDraft} running={!editable} disabled={!editable || saving} />}
          </fieldset>
          {section === 'network' && (result.preserved_network.hostfwd.length > 0 || result.preserved_network.extra_options) && <div className="settings-preserved"><Label>沿用的 profile 网络参数</Label><p className="path-value">{[result.preserved_network.extra_options, ...result.preserved_network.hostfwd].filter(Boolean).join('\n')}</p></div>}
          {deletedIds.length > 0 && <p role="status" className="field-hint">保存时将删除 {deletedIds.length} 个受管磁盘文件。取消设置可放弃删除。</p>}
          {error && <p role="alert" className="text-destructive settings-error">{error}</p>}
        </div>
      </div>}
      <DialogFooter><Button variant="secondary" disabled={saving} onClick={close}>{editable ? '取消' : '关闭'}</Button>{supported && result && <Button disabled={!editable || !dirty || saving} onClick={() => void save(true)}>{saving ? '保存中…' : '保存'}</Button>}</DialogFooter>
    </DialogContent></Dialog>
    {creating && result && <CreateDrive caps={result.capabilities} onAdd={addDrive} onClose={() => setCreating(false)} />}
    <Dialog open={!!removing} onOpenChange={value => { if (!value) setRemoving(null) }}><DialogContent><DialogHeader><DialogTitle>删除驱动器？</DialogTitle><DialogDescription>{removing && result?.managed_disks?.[removing.id] ? '保存设置后将永久删除此磁盘文件及其中数据。取消设置会保留磁盘。' : '此设备将从配置中移除，外部源文件与 ISO 保留。取消设置可放弃删除。'}</DialogDescription></DialogHeader>{removing && <><p>{removing.label}</p>{result?.managed_disks?.[removing.id] && <p className="path-value">{result.managed_disks[removing.id].path}</p>}</>}<DialogFooter><Button variant="secondary" onClick={() => setRemoving(null)}>取消</Button><Button variant="destructive" onClick={remove}>删除</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={leave} onOpenChange={setLeave}><DialogContent><DialogHeader><DialogTitle>保存配置修改？</DialogTitle><DialogDescription>离开前可以保存修改，或丢弃本次编辑。</DialogDescription></DialogHeader><DialogFooter><Button variant="ghost" disabled={saving} onClick={() => setLeave(false)}>继续编辑</Button><Button variant="secondary" disabled={saving} onClick={onClose}>丢弃修改</Button><Button disabled={saving || !editable} onClick={() => void save(true)}>保存并关闭</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={resize != null} onOpenChange={value => { if (!value && !saving) setResize(null) }}><DialogContent><DialogHeader><DialogTitle>扩容磁盘</DialogTitle><DialogDescription>容量只能增大，完成后需在客户机中扩展分区。</DialogDescription></DialogHeader><div><Label htmlFor="resize-capacity">目标容量 / GiB</Label><Input id="resize-capacity" type="number" value={capacity} onChange={event => setCapacity(Number(event.target.value))} /></div><DialogFooter><Button variant="secondary" disabled={saving} onClick={() => setResize(null)}>取消</Button><Button disabled={saving || !Number.isInteger(capacity) || capacity > 4096 || capacity <= resizeMinimum} onClick={() => void expand()}>确认扩容</Button></DialogFooter></DialogContent></Dialog>
  </>
}
