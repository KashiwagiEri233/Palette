import { useId, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Checkbox } from '@/components/ui/checkbox'
import { Card, CardContent } from '@/components/ui/card'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog'
import type { AgentModelDefinition, AgentProviderView, AgentSettings, CustomAgentApiKind, CustomAgentProvider } from '../../shared/agent'
import { CopyDebugInfoButton } from './CopyDebugInfoButton'

const newModel = (): AgentModelDefinition => ({ id: '', name: '', contextWindow: 128000, maxTokens: 8192, vision: false, reasoning: false })

export function ProviderEditor({ provider, settings, onCancel, onSaved, onError }: {
  provider?: AgentProviderView
  settings: AgentSettings
  onCancel(): void
  onSaved(): Promise<void>
  onError(error: unknown): void
}) {
  const [kind, setKind] = useState(provider?.custom ? 'custom' : 'builtin')
  const [builtinId, setBuiltinId] = useState(provider?.custom ? 'openai' : provider?.id ?? 'openai')
  const [form, setForm] = useState<CustomAgentProvider>(() => ({ id: provider?.custom ? provider.id : '', name: provider?.custom ? provider.name : '', api: provider?.api ?? 'openai-completions', baseUrl: provider?.baseUrl ?? '', models: provider?.custom ? settings.models.filter(model => model.providerId === provider.id).map(({ id, name, contextWindow, maxTokens, vision, reasoning }) => ({ id, name, contextWindow, maxTokens, vision, reasoning })) : [] }))
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [candidates, setCandidates] = useState<AgentModelDefinition[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [query, setQuery] = useState('')
  const id = useId()
  const current = kind === 'custom' ? provider : settings.providers.find(item => item.id === builtinId)
  const configured = !!current?.credentialConfigured
  const patch = (value: Partial<CustomAgentProvider>) => { setForm(previous => ({ ...previous, ...value })); setError('') }
  const patchModel = (index: number, value: Partial<AgentModelDefinition>) => patch({ models: form.models.map((model, position) => position === index ? { ...model, ...value } : model) })
  const save = async () => {
    setBusy(true); setError('')
    try {
      if (kind === 'custom') await window.palette.request('agent.providers.save', undefined, { provider: form, ...(key.trim() ? { key } : {}) })
      else if (key.trim()) await window.palette.request('agent.credentials.set', undefined, { providerId: builtinId, key })
      else if (!configured) throw new Error('请填写 API Key')
      setKey('')
      await onSaved()
    } catch (error) { setError(error instanceof Error ? error.message : '保存失败，请重试') }
    finally { setBusy(false) }
  }
  const removeKey = async () => {
    if (!current) return
    setBusy(true)
    try { await window.palette.request('agent.credentials.remove', undefined, { providerId: current.id }); setKey(''); await onSaved() }
    catch (error) { onError(error) } finally { setBusy(false) }
  }
  const discover = async () => {
    setBusy(true); setError('')
    try {
      const result = await window.palette.request('agent.models.discover', undefined, { api: form.api, baseUrl: form.baseUrl, ...(form.id ? { providerId: form.id } : {}), ...(key.trim() ? { key } : {}) })
      setCandidates(result.models)
      setSelected(new Set(result.models.filter(model => form.models.some(saved => saved.id === model.id)).map(model => model.id)))
      setQuery('')
    } catch (error) { setError(error instanceof Error ? error.message : '获取模型失败，请手动添加') }
    finally { setBusy(false) }
  }
  const filtered = (candidates ?? []).filter(model => `${model.id} ${model.name}`.toLowerCase().includes(query.toLowerCase().trim()))
  const importModels = () => {
    const existing = new Set(form.models.map(model => model.id))
    patch({ models: [...form.models, ...(candidates ?? []).filter(model => selected.has(model.id) && !existing.has(model.id))] })
    setCandidates(null)
  }
  const validModels = form.models.every(model => model.id.trim() && model.name.trim()) && new Set(form.models.map(model => model.id.trim())).size === form.models.length
  return <Card className="agent-provider-editor"><CardContent>
    <fieldset disabled={busy}>
      <div className="agent-provider-toolbar"><h2>{provider ? `编辑 ${provider.name}` : '添加提供商'}</h2></div>
      {!provider && <Tabs value={kind} onValueChange={value => { setKind(String(value)); setKey(''); setError('') }}><TabsList aria-label="提供商类型"><TabsTrigger value="builtin">内置提供商</TabsTrigger><TabsTrigger value="custom">自定义 API</TabsTrigger></TabsList></Tabs>}
      {kind === 'builtin' ? <>
        <p className="field-hint">从内置目录选择提供商，保存 API Key 后，其模型会出现在聊天选择器中。</p>
        <div className="settings-form-row"><Label htmlFor={`${id}-provider`}>模型提供方</Label><FormSelect id={`${id}-provider`} disabled={!!provider} value={builtinId} onValueChange={value => { setBuiltinId(value); setKey('') }} options={settings.providers.filter(item => !item.custom).map(item => ({ value: item.id, label: item.name }))} /></div>
      </> : <>
        <div className="settings-form-row"><Label htmlFor={`${id}-name`}>提供商名称</Label><Input autoFocus id={`${id}-name`} maxLength={128} value={form.name} onChange={event => patch({ name: event.target.value })} placeholder="例如：我的模型服务" /></div>
        <div className="settings-form-row"><Label htmlFor={`${id}-api`}>API 类型</Label><FormSelect id={`${id}-api`} value={form.api} onValueChange={value => patch({ api: value as CustomAgentApiKind })} options={[{ value: 'openai-completions', label: 'OpenAI Chat Completions' }, { value: 'openai-responses', label: 'OpenAI Responses' }, { value: 'anthropic-messages', label: 'Anthropic Messages' }]} /></div>
        <div className="settings-form-row"><Label htmlFor={`${id}-endpoint`}>Base URL</Label><Input id={`${id}-endpoint`} placeholder={form.api === 'anthropic-messages' ? 'https://api.example.com' : 'https://api.example.com/v1'} value={form.baseUrl} onChange={event => patch({ baseUrl: event.target.value })} /></div>
      </>}
      <div className="settings-form-row"><Label htmlFor={`${id}-key`}>API Key</Label><div className="agent-provider-key"><Input id={`${id}-key`} type="password" autoComplete="off" maxLength={8192} value={key} placeholder={configured ? '已配置，留空保留原密钥' : '输入 API Key'} onChange={event => setKey(event.target.value)} />{configured && <Button variant="ghost" size="sm" onClick={() => void removeKey()}>移除密钥</Button>}</div></div>
      {kind === 'custom' && <section className="agent-provider-models" aria-label="提供商模型">
        <div className="agent-provider-toolbar"><h3>模型 <span className="field-hint">{form.models.length}</span></h3><div className="agent-provider-actions"><Button variant="outline" size="sm" disabled={!form.baseUrl.trim() || (!key.trim() && !configured)} onClick={() => void discover()}>{busy ? '正在获取…' : '获取模型'}</Button><Button variant="outline" size="sm" onClick={() => patch({ models: [...form.models, newModel()] })}>手动添加模型</Button></div></div>
        <p className="field-hint">获取后勾选需要的模型；能力和上下文参数可在高级设置中调整。</p>
        {!form.models.length && <p className="agent-provider-empty">尚未添加模型。可先保存提供商，添加模型并配置密钥后即可在聊天中选择。</p>}
        {form.models.map((model, index) => <details key={index} className="agent-model-editor" ref={element => { if (element && !model.id) element.open = true }}>
          <summary><span>{model.name || model.id || '新模型'}</span><small>{model.id && model.name !== model.id ? model.id : '模型设置'}</small></summary>
          <div className="agent-model-fields">
            <div className="settings-form-row"><Label htmlFor={`${id}-model-${index}`}>模型 ID</Label><Input id={`${id}-model-${index}`} maxLength={256} value={model.id} onChange={event => patchModel(index, { id: event.target.value, ...(!model.name || model.name === model.id ? { name: event.target.value } : {}) })} /></div>
            <div className="settings-form-row"><Label htmlFor={`${id}-display-${index}`}>显示名称</Label><Input id={`${id}-display-${index}`} maxLength={256} value={model.name} onChange={event => patchModel(index, { name: event.target.value })} /></div>
            <details className="agent-model-advanced"><summary>高级设置</summary>
              <div className="settings-form-row"><Label htmlFor={`${id}-context-${index}`}>上下文长度</Label><Input id={`${id}-context-${index}`} type="number" min={1024} max={10000000} value={model.contextWindow} onChange={event => patchModel(index, { contextWindow: Number(event.target.value) })} /></div>
              <div className="settings-form-row"><Label htmlFor={`${id}-output-${index}`}>最大输出</Label><Input id={`${id}-output-${index}`} type="number" min={1} max={model.contextWindow} value={model.maxTokens} onChange={event => patchModel(index, { maxTokens: Number(event.target.value) })} /></div>
              <div className="settings-form-row"><Label htmlFor={`${id}-vision-${index}`}>支持图像输入</Label><Switch id={`${id}-vision-${index}`} checked={model.vision} onCheckedChange={checked => patchModel(index, { vision: checked })} /></div>
              <div className="settings-form-row"><Label htmlFor={`${id}-reasoning-${index}`}>支持推理强度</Label><Switch id={`${id}-reasoning-${index}`} checked={model.reasoning} onCheckedChange={checked => patchModel(index, { reasoning: checked })} /></div>
            </details>
            <div className="agent-model-remove"><Button variant="ghost" size="sm" className="text-destructive" aria-label={`移除模型 ${model.name || index + 1}`} onClick={() => patch({ models: form.models.filter((_, position) => position !== index) })}>移除模型</Button></div>
          </div>
        </details>)}
      </section>}
      {error && <div role="alert" className="agent-provider-error"><span>{error}</span><CopyDebugInfoButton source="provider" error={error} /></div>}
      <div className="agent-settings-actions"><Button variant="outline" onClick={onCancel}>取消</Button><Button disabled={kind === 'custom' ? !form.name.trim() || !form.baseUrl.trim() || !validModels : !key.trim() && !configured} onClick={() => void save()}>保存提供商</Button></div>
    </fieldset>
    <Dialog open={candidates !== null} onOpenChange={open => { if (!open) setCandidates(null) }}><DialogContent className="agent-model-discovery"><DialogTitle>选择要添加的模型</DialogTitle><DialogDescription>仅将勾选的模型加入提供商，已保存的模型和能力参数会保留。</DialogDescription>
      <Input aria-label="搜索获取的模型" placeholder="搜索模型…" value={query} onChange={event => setQuery(event.target.value)} />
      <div className="agent-provider-toolbar"><span className="field-hint">已选 {selected.size} / {candidates?.length ?? 0}</span><Button variant="ghost" size="sm" onClick={() => setSelected(previous => { const next = new Set(previous); const all = filtered.every(model => next.has(model.id)); for (const model of filtered) { if (all) next.delete(model.id); else next.add(model.id) }; return next })}>{filtered.length > 0 && filtered.every(model => selected.has(model.id)) ? '取消全选' : '全选当前结果'}</Button></div>
      <div className="agent-model-candidates">{filtered.map(model => <label key={model.id} className="agent-model-candidate"><Checkbox aria-label={model.name} checked={selected.has(model.id)} onCheckedChange={checked => setSelected(previous => { const next = new Set(previous); if (checked) next.add(model.id); else next.delete(model.id); return next })} /><span>{model.name}<small>{model.id}</small></span></label>)}{!filtered.length && <p className="agent-provider-empty">没有找到模型，可返回后手动添加。</p>}</div>
      <div className="agent-settings-actions"><Button variant="outline" onClick={() => setCandidates(null)}>取消</Button><Button onClick={importModels} disabled={!selected.size}>添加所选模型</Button></div>
    </DialogContent></Dialog>
  </CardContent></Card>
}
