import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { FolderIcon } from '@/components/icons'
import { HardwareFields, ResourceInputs } from './VmSettings'
import { sections, validateVmSettings, type SettingsSection } from './lib/vm-settings'
import { archName, configurationDraft, creationPayload, creationResourceError, osNames } from './lib/os-presets'
import type { HardwareCapabilities, OsPresetCatalog, OsPresetId, Profile, VmDraft } from '../shared/types'

const errorText = (error: unknown): string => error instanceof Error ? error.message.replace(/^Error invoking remote method '[^']+': Error: /, '') : String(error)
const networkNames = { 'restricted-lab': '隔离网络', offline: '离线', nat: 'NAT' }
const empty: VmDraft = { os_preset: 'custom', name: '', profile_id: '', source: 'image', path: '', image_format: 'qcow2', vcpus: 2, memory_mib: 4096, disk_gib: 24, network: 'restricted-lab' }
interface Choice { id: OsPresetId; arch: string; profileId?: string }
interface Props { open: boolean; source: 'image' | 'iso'; onClose: () => void; profiles: Profile[]; defaultProfile: string; onCreated: (id: string) => Promise<void> }

export function CreateVm({ open, source, onClose, profiles, defaultProfile, onCreated }: Props) {
  const [step, setStep] = useState(0)
  const [draft, setDraft] = useState<VmDraft>(empty)
  const [catalog, setCatalog] = useState<OsPresetCatalog | null>(null)
  const [mode, setMode] = useState<'preset' | 'custom'>('preset')
  const [arch, setArch] = useState('x86_64')
  const [emulation, setEmulation] = useState(false)
  const [error, setError] = useState('')
  const [catalogError, setCatalogError] = useState('')
  const [saving, setSaving] = useState(false)
  const [advanced, setAdvanced] = useState(false)
  const [hardwareSection, setHardwareSection] = useState<SettingsSection>('display')
  const [caps, setCaps] = useState<HardwareCapabilities | null>(null)
  const [capsError, setCapsError] = useState('')
  const [image, setImage] = useState<{ format: string; virtual_size: number } | null>(null)
  const [imageError, setImageError] = useState('')
  const [isoAttached, setIsoAttached] = useState(true)
  const [leaving, setLeaving] = useState(false)
  const [pending, setPending] = useState<Choice | null>(null)
  const modified = useRef(false)
  const latest = useRef({ profiles, defaultProfile, source })
  latest.current = { profiles, defaultProfile, source }
  const profile = profiles.find(item => item.profile_id === draft.profile_id)
  const selected = catalog?.presets.find(item => item.id === draft.os_preset)
  const target = selected?.targets.find(item => item.arch === arch)
  const configured = mode === 'custom' ? !!profile : !!selected && !!target?.available && (!target.requires_emulation || emulation)
  const minimumDisk = image ? Math.ceil(image.virtual_size / 1024 ** 3) : 0

  useEffect(() => {
    if (!open) return
    const initial = latest.current
    const base = initial.profiles.find(item => item.profile_id === initial.defaultProfile) ?? initial.profiles[0]
    setDraft(base ? configurationDraft(base, { ...empty, source: initial.source }) : { ...empty, source: initial.source })
    setStep(0); setError(''); setCatalog(null); setCatalogError(''); setCaps(null); setCapsError('')
    setMode('preset')
    setArch(base?.qemu.arch ?? 'x86_64'); setEmulation(false); setAdvanced(false); setHardwareSection('display')
    setIsoAttached(true); setLeaving(false); setPending(null); modified.current = false
    let live = true
    void window.palette.request('os_presets.list').then(data => {
      if (live) { const result = data as OsPresetCatalog; setCatalog(result); setArch(result.host_arch === 'aarch64' ? 'aarch64' : 'x86_64') }
    }).catch(reason => { if (live) setCatalogError(errorText(reason)) })
    return () => { live = false }
  }, [open])

  useEffect(() => {
    if (!open || !draft.profile_id) return
    let live = true
    setCaps(null); setCapsError('')
    void window.palette.request('hardware.capabilities', undefined, { profile_id: draft.profile_id }).then(data => { if (live) setCaps(data as HardwareCapabilities) }).catch(reason => { if (live) setCapsError(errorText(reason)) })
    return () => { live = false }
  }, [open, draft.profile_id])

  useEffect(() => {
    setImage(null); setImageError('')
    if (!open || draft.source !== 'image' || !draft.path || !draft.profile_id) return
    let live = true
    void window.palette.request('image.info', undefined, { profile_id: draft.profile_id, path: draft.path }).then(data => { if (live) setImage(data as typeof image) }).catch(reason => { if (live) setImageError(errorText(reason)) })
    return () => { live = false }
  }, [open, draft.source, draft.path, draft.profile_id])

  function change<K extends keyof VmDraft>(key: K, value: VmDraft[K]): void {
    modified.current = true; setError('')
    setDraft(previous => ({ ...previous, [key]: value }))
  }
  function apply(choice: Choice): void {
    const preset = catalog?.presets.find(item => item.id === choice.id)
    const nextTarget = preset?.targets.find(item => item.arch === choice.arch)
    const nextProfile = profiles.find(item => item.profile_id === (choice.id === 'custom' ? choice.profileId ?? defaultProfile : nextTarget?.profile_id)) ?? (choice.id === 'custom' ? profiles[0] : undefined)
    if (!nextProfile || (choice.id !== 'custom' && !nextTarget?.available)) {
      if (preset && nextTarget) {
        setMode('preset'); setArch(choice.arch); setEmulation(false); setPending(null)
        setDraft(previous => ({ ...previous, os_preset: preset.id, profile_id: '' }))
        modified.current = false
      }
      setError(nextTarget?.reason ?? '没有匹配的系统配置'); return
    }
    setDraft(previous => configurationDraft(nextProfile, previous, preset, nextTarget))
    setMode(choice.id === 'custom' ? 'custom' : 'preset'); setArch(nextProfile.qemu.arch)
    setEmulation(false); setIsoAttached(true); setError(''); setPending(null); modified.current = false
  }
  function choose(choice: Choice): void { if (modified.current) setPending(choice); else apply(choice) }
  function close(): void { if (!saving) { if (draft.path || draft.name || modified.current) setLeaving(true); else onClose() } }
  async function browse(): Promise<void> {
    try {
      const path = await window.palette.chooseFile(draft.source)
      if (!path) return
      modified.current = true; setError('')
      setDraft(previous => ({ ...previous, path, image_format: /\.(raw|img)$/i.test(path) ? 'raw' : 'qcow2', settings: previous.settings ? { ...previous.settings, iso_path: previous.source === 'iso' ? path : previous.settings.iso_path } : undefined }))
      if (draft.source === 'iso') setIsoAttached(true)
    } catch (reason) { setError(errorText(reason)) }
  }
  function changeSource(value: VmDraft['source']): void {
    if (value === draft.source) return
    modified.current = true; setError('')
    setDraft(previous => ({ ...previous, source: value, path: '', settings: previous.settings ? { ...previous.settings, iso_path: null, boot_order: 'auto' } : undefined }))
    setIsoAttached(true)
  }
  function sourceProblem(): string | null {
    if (!draft.path) return '请选择来源文件。'
    if (draft.source === 'image') {
      if (imageError) return imageError
      if (!image) return '正在读取镜像信息，请稍候。'
      if (image.format !== draft.image_format) return `请选择镜像的实际格式：${image.format}。`
    }
    return null
  }
  function configurationProblem(): boolean {
    const invalid = validateVmSettings(draft, draft.settings, isoAttached)
    const resourceError = creationResourceError(draft, caps, minimumDisk)
    const unsupported = caps?.available ? (
      !caps.displays.some(item => item.model === draft.settings?.display.model) ? '所选显卡不受支持，请在更多硬件选项中调整。' : null
    ) : null
    const message = invalid?.message ?? resourceError ?? (capsError || (!caps ? '正在检查硬件能力，请稍候。' : !caps.available ? caps.reason : unsupported))
    if (!message) return false
    setStep(2); setError(message)
    if (invalid && ['disk', 'display', 'network', 'audio'].includes(invalid.section)) { setAdvanced(true); setHardwareSection(invalid.section) }
    return true
  }
  function next(): void {
    setError('')
    if (step === 0 && !configured) return
    if (step === 1) { const problem = sourceProblem(); if (problem) { setError(problem); return } }
    if (step === 2 && configurationProblem()) return
    setStep(step + 1)
  }
  async function create(): Promise<void> {
    if (!configured) { setStep(0); setLeaving(false); return }
    const problem = sourceProblem()
    if (problem) { setStep(1); setLeaving(false); setError(problem); return }
    if (configurationProblem()) { setLeaving(false); return }
    setSaving(true); setError('')
    try { const result = await window.palette.request('vms.create', undefined, creationPayload(draft, isoAttached)) as { vm_id: string }; await onCreated(result.vm_id) }
    catch (reason) { setLeaving(false); setError(errorText(reason)) }
    finally { setSaving(false) }
  }

  return <><Dialog open={open} onOpenChange={value => { if (!value) close() }}><DialogContent className="create-dialog" scrollable>
    <DialogHeader><DialogTitle>{['选择配置', '选择来源', '配置虚拟机', '确认创建'][step]}</DialogTitle><DialogDescription>{['选择操作系统预设，或使用自定义配置。', '选择已有磁盘镜像，或使用 ISO 安装系统。', '设置 CPU、内存和磁盘容量。', '确认配置后创建虚拟机。'][step]}</DialogDescription></DialogHeader>
    <ol className="wizard-steps" aria-label="创建步骤">{['系统', '来源', '配置', '确认'].map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined} className={index <= step ? 'reached' : ''}><span>{index + 1}</span>{label}</li>)}</ol>
    {step === 0 && <div className="form-stack">
      <div className="source-options" aria-label="配置方式"><Button variant={mode === 'preset' ? 'default' : 'secondary'} aria-pressed={mode === 'preset'} onClick={() => setMode('preset')}>预设</Button><Button variant={mode === 'custom' ? 'default' : 'secondary'} aria-pressed={mode === 'custom'} onClick={() => { if (mode !== 'custom') choose({ id: 'custom', arch }) }}>自定义</Button></div>
      {mode === 'preset' ? <>
        <div className="os-preset-options" aria-label="操作系统预设">{catalog?.presets.map(preset => <Button key={preset.id} aria-label={`${preset.name} 预设`} aria-pressed={draft.os_preset === preset.id} variant={draft.os_preset === preset.id ? 'default' : 'outline'} className="os-preset-option" onClick={() => choose({ id: preset.id, arch })}><strong>{preset.name}</strong><span>{preset.vcpus} 核 · {preset.memory_mib / 1024} GiB 内存 · {preset.disk_gib} GiB 磁盘</span></Button>)}</div>
        {!catalog && <p role="status" className="field-hint">{catalogError || '正在加载系统预设…'}</p>}
        <div><Label htmlFor="preset-arch">客户机架构</Label><FormSelect id="preset-arch" value={arch} disabled={!catalog} onValueChange={value => { if (selected) choose({ id: selected.id, arch: value }); else setArch(value) }} options={[{ value: 'x86_64', label: 'x86_64' }, { value: 'aarch64', label: 'ARM64' }]} /></div>
        {target?.reason && <p className="field-hint" role="status">{target.reason}</p>}
        {target?.requires_emulation && <label className="create-emulation"><input type="checkbox" checked={emulation} onChange={event => setEmulation(event.target.checked)} />使用软件模拟（TCG，运行速度较慢）</label>}
        <p className="field-hint">预设提供可编辑的建议值。请使用与客户机架构和驱动匹配的系统介质。</p>
      </> : <div><Label htmlFor="vm-profile">系统配置</Label><FormSelect id="vm-profile" value={draft.profile_id} disabled={!profiles.length} onValueChange={value => choose({ id: 'custom', arch, profileId: value })} options={profiles.map(item => ({ value: item.profile_id, label: `${item.profile_id} / ${archName(item.qemu.arch)} / ${item.qemu.accelerator}` }))} /></div>}
      {profile && <p className="field-hint">{archName(profile.qemu.arch)} · {profile.qemu.accelerator} · {profile.profile_id}</p>}
    </div>}
    {step === 1 && <div className="form-stack">
      <div className="source-options"><Button variant={draft.source === 'image' ? 'default' : 'secondary'} onClick={() => changeSource('image')}>磁盘镜像</Button><Button variant={draft.source === 'iso' ? 'default' : 'secondary'} onClick={() => changeSource('iso')}>ISO 安装</Button></div>
      <div><Label htmlFor="source-path">{draft.source === 'iso' ? 'ISO 文件' : '镜像文件'}</Label><div className="file-field"><Input id="source-path" value={draft.path} readOnly placeholder="选择文件" /><Button variant="secondary" onClick={() => void browse()}><FolderIcon className="size-4" />浏览</Button></div></div>
      {draft.source === 'image' && <><div><Label htmlFor="image-format">镜像格式</Label><FormSelect id="image-format" value={draft.image_format} onValueChange={value => change('image_format', value as 'qcow2' | 'raw')} options={[{ value: 'qcow2', label: 'qcow2' }, { value: 'raw', label: 'raw' }]} /></div><p role="status" className="field-hint">{imageError || (image ? `镜像虚拟容量：${(image.virtual_size / 1024 ** 3).toFixed(2)} GiB` : draft.path ? '正在读取镜像信息…' : '导入镜像需与所选架构、启动方式及存储驱动匹配。')}</p></>}
    </div>}
    {step === 2 && <div className="form-stack">
      <div><Label htmlFor="vm-name">名称</Label><Input id="vm-name" autoFocus maxLength={120} value={draft.name} onChange={event => change('name', event.target.value)} placeholder="例如：开发环境" /></div>
      <ResourceInputs draft={draft} change={change} />
      <div><Label htmlFor="disk">磁盘 / GiB</Label><Input id="disk" type="number" min={Math.max(1, minimumDisk)} max={4096} value={draft.disk_gib} onChange={event => change('disk_gib', Number(event.target.value))} />{minimumDisk > 0 && <p className="field-hint">镜像需要至少 {minimumDisk} GiB。</p>}</div>
      <div><Label htmlFor="network">网络</Label><FormSelect id="network" value={draft.network} onValueChange={value => change('network', value as VmDraft['network'])} options={Object.entries(networkNames).map(([value, label]) => ({ value, label }))} /></div>
      {!caps && <p role="status" className="field-hint">{capsError || '正在检查硬件能力…'}</p>}
      {caps && <p className="field-hint">宿主资源：{caps.host.vcpus ?? '—'} 核 / {(caps.host.memory_mib / 1024).toFixed(1)} GiB 内存。</p>}
      <div className="create-advanced"><Button variant="ghost" aria-expanded={advanced} onClick={() => setAdvanced(value => !value)}>{advanced ? '收起硬件选项' : '更多硬件选项'}</Button>{advanced && draft.settings && <><div className="hardware-tabs">{sections.filter(item => ['disk', 'display', 'network', 'audio'].includes(item.id)).map(item => <Button key={item.id} variant="ghost" aria-pressed={hardwareSection === item.id} onClick={() => setHardwareSection(item.id)}>{item.label}</Button>)}</div><HardwareFields section={hardwareSection} settings={draft.settings} change={settings => change('settings', settings)} caps={caps} attached={isoAttached} setAttached={setIsoAttached} draft={draft} setDraft={value => { modified.current = true; setDraft(value) }} includeMode={false} /></>}</div>
    </div>}
    {step === 3 && <dl className="configuration-grid"><div><dt>名称</dt><dd>{draft.name}</dd></div><div><dt>系统预设</dt><dd>{osNames[draft.os_preset ?? 'custom']}</dd></div><div><dt>架构 / 加速</dt><dd>{archName(profile?.qemu.arch ?? arch)} / {profile?.qemu.accelerator}</dd></div><div><dt>CPU / 内存</dt><dd>{draft.vcpus} 核 / {draft.memory_mib / 1024} GiB</dd></div><div><dt>磁盘容量</dt><dd>{draft.disk_gib} GiB</dd></div><div><dt>网络</dt><dd>{networkNames[draft.network]}</dd></div>{draft.settings && <><div><dt>显示</dt><dd>{draft.settings.display.model}</dd></div><div><dt>声音</dt><dd>{draft.settings.audio_enabled ? '启用' : '关闭'}</dd></div><div><dt>启动顺序</dt><dd>{{ auto: '自动', disk: '硬盘优先', cdrom: '光盘优先' }[draft.settings.boot_order]}</dd></div></>}<div className="col-span-2"><dt>来源 · {draft.source === 'iso' ? 'ISO 安装' : '磁盘镜像'}</dt><dd className="path-value">{draft.path}</dd></div>{draft.settings?.iso_path && <div className="col-span-2"><dt>安装介质 · {isoAttached ? '已挂载' : '未挂载'}</dt><dd className="path-value">{draft.settings.iso_path}</dd></div>}</dl>}
    {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
    <DialogFooter><Button variant="secondary" disabled={saving} onClick={() => { setError(''); if (step) setStep(step - 1); else close() }}>{step ? '上一步' : '取消'}</Button>{step < 3 ? <Button disabled={step === 0 ? !configured : step === 1 ? !draft.path || (draft.source === 'image' && !image && !imageError) : !caps && !capsError} onClick={next}>下一步</Button> : <Button disabled={saving} onClick={() => void create()}>{saving ? '创建中…' : '创建虚拟机'}</Button>}</DialogFooter>
  </DialogContent></Dialog>
    <Dialog open={!!pending} onOpenChange={value => { if (!value) setPending(null) }}><DialogContent><DialogHeader><DialogTitle>更换配置？</DialogTitle><DialogDescription>CPU、内存、磁盘容量和硬件选项将填入新配置的建议值，名称和来源文件会保留。</DialogDescription></DialogHeader><DialogFooter><Button variant="secondary" onClick={() => setPending(null)}>继续编辑</Button><Button onClick={() => { if (pending) apply(pending) }}>更换配置</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={leaving} onOpenChange={setLeaving}><DialogContent><DialogHeader><DialogTitle>保留新建配置？</DialogTitle><DialogDescription>可以继续编辑、丢弃配置，或直接创建虚拟机。</DialogDescription></DialogHeader><DialogFooter><Button variant="ghost" disabled={saving} onClick={() => setLeaving(false)}>继续编辑</Button><Button variant="secondary" disabled={saving} onClick={() => { setLeaving(false); onClose() }}>丢弃配置</Button><Button disabled={saving || !configured || !draft.path || !draft.name.trim()} onClick={() => void create()}>创建虚拟机</Button></DialogFooter></DialogContent></Dialog>
  </>
}
