import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { FolderIcon, PlusIcon } from '@/components/icons'
import { DriveFields } from './DriveFields'
import type { HardwareCapabilities, HardwareSettings, OwnerInfo, VmDraft, VmRecord, VmSettingsResult } from '../shared/types'
import { displayNames, networkNames, sections, sizeLabel, validateVmSettings, errorSection, type SettingsSection } from './lib/vm-settings'

const errorText = (error: unknown): string => String(error instanceof Error ? error.message : error).replace(/^Error invoking remote method '[^']+': Error: /, '')
const resolutions = ['1024x768', '1280x800', '1366x768', '1920x1080', '2560x1440', '3840x2160']

export function ResourceInputs({ draft, change }: { draft: VmDraft; change: (key: 'vcpus' | 'memory_mib', value: number) => void }) {
  return <div className="resource-fields"><div><Label htmlFor="settings-cpu">CPU 核数</Label><Input id="settings-cpu" type="number" min={1} max={64} value={draft.vcpus} onChange={event => change('vcpus', Number(event.target.value))} /></div><div><Label htmlFor="settings-memory">内存 / GiB</Label><Input id="settings-memory" type="number" min={0.25} max={1024} step={0.25} value={draft.memory_mib / 1024} onChange={event => change('memory_mib', Number(event.target.value) * 1024)} /></div></div>
}

export function HardwareFields({ section, settings, change, caps, attached, setAttached, draft, setDraft, includeMode = true, running = false, disabled = false }: {
  section: SettingsSection; settings: HardwareSettings; change: (settings: HardwareSettings) => void; caps: HardwareCapabilities | null;
  attached: boolean; setAttached: (value: boolean) => void; draft: VmDraft; setDraft: (draft: VmDraft) => void; includeMode?: boolean; running?: boolean; disabled?: boolean
}) {
  const [fileError, setFileError] = useState('')
  const display = settings.display
  const resolutionSupported = caps?.displays.find(item => item.model === display.model)?.resolution ?? false
  const network = settings.network
  const [customResolution, setCustomResolution] = useState(false)
  return <div className="form-stack">
    {section === 'disk' && <>
      <div><Label htmlFor="settings-iso">安装介质</Label><div className="file-field"><Input id="settings-iso" readOnly value={settings.iso_path ?? ''} placeholder="未选择 ISO" /><Button variant="secondary" onClick={() => void window.palette.chooseFile('iso').then(path => { if (path) { change({ ...settings, iso_path: path }); setAttached(true); setFileError('') } }).catch(error => setFileError(errorText(error)))}><FolderIcon className="size-4" />选择 ISO</Button></div></div>
      {fileError && <p role="alert" className="text-destructive">{fileError}</p>}
      <div><Label htmlFor="settings-iso-state">介质状态</Label><FormSelect id="settings-iso-state" disabled={disabled} value={attached && settings.iso_path ? 'attached' : 'ejected'} onValueChange={value => { const next = value === 'attached'; setAttached(next); if (!next && settings.boot_order === 'cdrom') change({ ...settings, boot_order: 'auto' }) }} options={[{ value: 'ejected', label: '未挂载' }, { value: 'attached', label: '挂载 ISO', disabled: !settings.iso_path }]} /></div>
      <div><Label htmlFor="boot-order">启动顺序</Label><FormSelect id="boot-order" disabled={disabled} value={settings.boot_order} onValueChange={value => change({ ...settings, boot_order: value as HardwareSettings['boot_order'] })} options={[{ value: 'auto', label: '自动' }, { value: 'disk', label: '硬盘优先' }, { value: 'cdrom', label: '光盘优先', disabled: !settings.iso_path || !attached }]} /><p className="field-hint">自动模式在首次启动时优先安装介质，后续优先硬盘。已保存的 UEFI 启动项仍可能影响系统选择。</p></div>
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
  const supported = owner?.features.includes('vm-settings-v2') ?? false
  const [result, setResult] = useState<VmSettingsResult | null>(null)
  const [draft, setDraft] = useState<VmDraft | null>(null)
  const [settings, setSettings] = useState<HardwareSettings | null>(null)
  const [attached, setAttached] = useState(false)
  const [section, setSection] = useState<SettingsSection>('general')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [leave, setLeave] = useState(false)
  const [resize, setResize] = useState(false)
  const [capacity, setCapacity] = useState(0)
  const [notice, setNotice] = useState('')
  const dirty = !!result && JSON.stringify({ draft, settings, attached }) !== JSON.stringify({ draft: result.draft, settings: result.settings, attached: result.iso_attached })
  const editable = !!result?.editable && ['STOPPED', 'FAILED'].includes(vm.status.state)
  function apply(data: VmSettingsResult): void { setResult(data); setDraft(data.draft); setSettings(data.settings); setAttached(data.iso_attached); setCapacity(Math.ceil(Math.max(data.disk.configured_gib, (data.disk.virtual_bytes ?? 0) / 1024 ** 3)) + 1) }
  useEffect(() => { let live = true; if (supported) void window.palette.request('vms.settings.get', vm.vm_id).then(data => { if (live) apply(data as VmSettingsResult) }).catch(error => { if (live) setError(errorText(error)) }); return () => { live = false } }, [vm.vm_id, supported])
  function close(): void { if (!saving) { if (dirty) setLeave(true); else onClose() } }
  async function save(exit = false): Promise<void> {
    if (!editable || !result || !draft || !settings) return
    const invalid = validateVmSettings(draft, settings, attached)
    if (invalid) { setSection(invalid.section); setError(invalid.message); setLeave(false); requestAnimationFrame(() => document.getElementById(invalid.field)?.focus()); return }
    setSaving(true); setError('')
    try {
      await window.palette.request('vms.update', vm.vm_id, { revision: result.revision, name: draft.name.trim(), vcpus: draft.vcpus, memory_mib: draft.memory_mib, network: draft.network, settings, iso_attached: attached })
      await onSaved(); if (exit) onClose()
    } catch (error) { const message = errorText(error); setLeave(false); setError(message); const target = errorSection(message); if (target) { setSection(target); requestAnimationFrame(() => document.querySelector<HTMLElement>('.settings-fieldset input, .settings-fieldset [data-slot=select-trigger]')?.focus()) } }
    finally { setSaving(false) }
  }
  async function expand(): Promise<void> {
    if (!editable || !result) return
    setSaving(true); setError('')
    try {
      await window.palette.request('vms.disk.resize', vm.vm_id, { revision: result.revision, disk_gib: capacity })
      apply(await window.palette.request('vms.settings.get', vm.vm_id) as VmSettingsResult)
      setResize(false); setNotice('磁盘已扩容。请在客户机中扩展分区。')
      await onSaved()
    } catch (error) { setError(errorText(error)); setResize(false) }
    finally { setSaving(false) }
  }
  return <>
    <Dialog open onOpenChange={open => { if (!open) close() }}><DialogContent className="vm-settings-dialog"><DialogHeader><DialogTitle>虚拟机设置</DialogTitle><DialogDescription>{vm.name}{result && !editable ? ' · 关机后可修改硬件配置' : ' · 修改在下次启动时生效'}</DialogDescription></DialogHeader>
      {!supported ? <p role="status" className="settings-upgrade">当前后台需要升级才能管理完整配置。请在关闭虚拟机后更新并重新启动后台，再重新连接。</p> : !result || !draft || !settings ? <p role="status">{error || '正在读取配置…'}</p> : <div className="vm-settings-layout">
        <nav className="vm-settings-navigation" aria-label="设置分类">{sections.map(item => <Button key={item.id} variant="ghost" aria-pressed={section === item.id} onClick={() => { setSection(item.id); setError('') }}>{item.label}</Button>)}<div className="vm-settings-summary"><strong>{draft.name || vm.name}</strong><span>{draft.vcpus} 核 · {draft.memory_mib / 1024} GiB</span><span>{draft.disk_gib} GiB · {networkNames[draft.network]}</span></div></nav>
        <div className="vm-settings-body"><h2>{sections.find(item => item.id === section)?.label}</h2>{notice && <p role="status" className="settings-notice">{notice}</p>}
          <fieldset disabled={!editable || saving} className="settings-fieldset">
            {section === 'general' && <div className="form-stack"><div><Label htmlFor="edit-name">名称</Label><Input id="edit-name" maxLength={120} value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} /></div><dl className="configuration-grid"><div><dt>系统配置</dt><dd>{draft.profile_id}</dd></div><div><dt>架构</dt><dd>{vm.status.guest_arch ?? '沿用原配置'}</dd></div></dl><div><Label>原始来源</Label><p className="path-value">{draft.path}</p><p className="field-hint">原始镜像保持只读，虚拟机修改保存在独立磁盘中。</p></div></div>}
            {section === 'resources' && <div className="form-stack"><ResourceInputs draft={draft} change={(key, value) => setDraft({ ...draft, [key]: value })} /><p className="field-hint">宿主资源：{result.capabilities.host.vcpus ?? '—'} 个逻辑核心 / {sizeLabel(result.capabilities.host.memory_mib * 1024 ** 2)} 内存。设置较高资源可能影响其他应用。</p></div>}
            {section === 'disk' && <div className="settings-disk-section"><dl className="configuration-grid"><div><dt>配置容量</dt><dd>{result.disk.configured_gib} GiB</dd></div><div><dt>实盘容量</dt><dd>{sizeLabel(result.disk.virtual_bytes)}</dd></div><div><dt>宿主占用</dt><dd>{sizeLabel(result.disk.actual_bytes)}</dd></div></dl><Button variant="outline" disabled={dirty || !owner?.features.includes('disk-resize')} onClick={() => setResize(true)}>扩容磁盘</Button><p className="field-hint">仅支持增大容量；扩容后需在客户机内扩展分区。{dirty ? '请先保存或丢弃其他修改，再扩容。' : ''}</p></div>}
            {section === 'devices' && <DriveFields settings={settings} change={setSettings} />}
            {['disk', 'display', 'network', 'audio'].includes(section) && <HardwareFields section={section} settings={settings} change={setSettings} caps={result.capabilities} attached={attached} setAttached={setAttached} draft={draft} setDraft={setDraft} running={!editable} disabled={!editable || saving} />}
          </fieldset>
          {section === 'network' && (result.preserved_network.hostfwd.length > 0 || result.preserved_network.extra_options) && <div className="settings-preserved"><Label>沿用的 profile 网络参数</Label><p className="path-value">{[result.preserved_network.extra_options, ...result.preserved_network.hostfwd].filter(Boolean).join('\n')}</p><p className="field-hint">这些已有参数会保留；修改它们需要编辑可信 profile。</p></div>}
          {error && <p role="alert" className="text-destructive settings-error">{error}</p>}
        </div>
      </div>}
      <DialogFooter><Button variant="secondary" disabled={saving} onClick={close}>{editable ? '取消' : '关闭'}</Button>{supported && result && <Button disabled={!editable || !dirty || saving} onClick={() => void save(true)}>{saving ? '保存中…' : '保存'}</Button>}</DialogFooter>
    </DialogContent></Dialog>
    <Dialog open={leave} onOpenChange={setLeave}><DialogContent><DialogHeader><DialogTitle>保存配置修改？</DialogTitle><DialogDescription>离开前可以保存修改，或丢弃本次编辑。</DialogDescription></DialogHeader><DialogFooter><Button variant="ghost" disabled={saving} onClick={() => setLeave(false)}>继续编辑</Button><Button variant="secondary" disabled={saving} onClick={onClose}>丢弃修改</Button><Button disabled={saving || !editable} onClick={() => void save(true)}>保存并关闭</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={resize} onOpenChange={open => { if (!saving) setResize(open) }}><DialogContent><DialogHeader><DialogTitle>扩容磁盘</DialogTitle><DialogDescription>磁盘容量只能增大，此操作不会自动扩展客户机分区。</DialogDescription></DialogHeader><div><Label htmlFor="resize-capacity">目标容量 / GiB</Label><Input id="resize-capacity" type="number" min={(result?.disk.configured_gib ?? 0) + 1} max={4096} value={capacity} onChange={event => setCapacity(Number(event.target.value))} /></div><DialogFooter><Button variant="secondary" disabled={saving} onClick={() => setResize(false)}>取消</Button><Button disabled={saving || !Number.isInteger(capacity) || capacity > 4096 || capacity <= Math.max(result?.disk.configured_gib ?? 0, (result?.disk.virtual_bytes ?? 0) / 1024 ** 3)} onClick={() => void expand()}>{saving ? '扩容中…' : '确认扩容'}</Button></DialogFooter></DialogContent></Dialog>
  </>
}
