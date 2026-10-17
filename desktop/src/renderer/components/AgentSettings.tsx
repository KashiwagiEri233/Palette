import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { useAgentSettings } from '../lib/use-agent'
import type { AgentConfiguration, AgentApiKind, AgentEffort } from '../../shared/agent'

export function AgentSettingsForm({ onError }: { onError(error: unknown): void }) {
  const { settings, reload } = useAgentSettings()
  const [form, setForm] = useState<AgentConfiguration | null>(null)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState('')
  const [credentialStatus, setCredentialStatus] = useState<{ providerId: string; configured: boolean } | null>(null)
  useEffect(() => { if (settings) setForm(previous => previous ?? settings.configuration) }, [settings])
  if (!settings || !form) return <p role="status">正在读取 Agent 设置…</p>
  const patch = (value: Partial<AgentConfiguration>) => { setForm({ ...form, ...value }); setSaved('') }
  const providerId = form.custom ? 'palette-custom' : form.providerId
  const configured = credentialStatus?.providerId === providerId ? credentialStatus.configured : providerId === (settings.configuration.custom ? 'palette-custom' : settings.configuration.providerId) && settings.credentialConfigured
  const models = settings.models.filter(model => model.providerId === form.providerId)
  const selectModel = (id: string, provider = form.providerId) => {
    const model = settings.models.find(model => model.providerId === provider && model.id === id)
    if (model) patch({ providerId: provider, modelId: id, api: model.api, baseUrl: model.baseUrl, contextWindow: model.contextWindow, maxTokens: model.maxTokens, vision: model.vision, reasoning: model.reasoning, custom: false })
  }
  const save = async () => {
    setBusy(true)
    try {
      await window.palette.request('agent.settings.update', undefined, { configuration: form })
      await reload()
      setSaved('设置已保存')
      window.dispatchEvent(new Event('palette-agent-settings'))
    } catch (error) { onError(error) } finally { setBusy(false) }
  }
  const credential = async (remove = false) => {
    setBusy(true)
    try {
      if (remove) await window.palette.request('agent.credentials.remove', undefined, { providerId })
      else await window.palette.request('agent.credentials.set', undefined, { providerId, key })
      setCredentialStatus({ providerId, configured: !remove })
      setKey('')
      window.dispatchEvent(new Event('palette-agent-settings'))
      setSaved(remove ? 'API Key 已移除' : 'API Key 已配置')
    } catch (error) { onError(error) } finally { setBusy(false) }
  }
  return <fieldset disabled={busy} className="agent-settings-fields">
    <p className="field-hint">内置 pi Agent 支持对话和虚拟机视觉操作。客户机命令执行与文件读写未接入。</p>
    <div className="settings-form-row"><Label htmlFor="agent-provider">模型提供方</Label><FormSelect id="agent-provider" value={form.custom ? 'custom' : form.providerId} options={[...settings.providers.map(provider => ({ value: provider.id, label: provider.name })), { value: 'custom', label: '自定义端点' }]} onValueChange={value => {
      if (value === 'custom') patch({ custom: true, api: 'openai-completions', modelId: '', baseUrl: '' })
      else { const first = settings.models.find(model => model.providerId === value); if (first) selectModel(first.id, first.providerId) }
    }} /></div>
    {form.custom ? <><div className="settings-form-row"><Label htmlFor="agent-api">API 类型</Label><FormSelect id="agent-api" value={form.api} onValueChange={value => patch({ api: value as AgentApiKind })} options={[{ value: 'openai-completions', label: 'OpenAI Chat Completions' }, { value: 'openai-responses', label: 'OpenAI Responses' }, { value: 'anthropic-messages', label: 'Anthropic Messages' }]} /></div>
      <div className="settings-form-row"><Label htmlFor="agent-endpoint">Base URL</Label><Input id="agent-endpoint" placeholder="https://api.example.com/v1" value={form.baseUrl} onChange={event => patch({ baseUrl: event.target.value })} /></div>
      <div className="settings-form-row"><Label htmlFor="agent-model">模型 ID</Label><Input id="agent-model" value={form.modelId} onChange={event => patch({ modelId: event.target.value })} /></div>
      <div className="settings-form-row"><Label htmlFor="agent-context">上下文长度</Label><Input id="agent-context" type="number" min={1024} max={10000000} value={form.contextWindow} onChange={event => patch({ contextWindow: Number(event.target.value) })} /></div>
      <div className="settings-form-row"><Label htmlFor="agent-output">最大输出</Label><Input id="agent-output" type="number" min={1} max={form.contextWindow} value={form.maxTokens} onChange={event => patch({ maxTokens: Number(event.target.value) })} /></div>
      <div className="settings-form-row"><Label htmlFor="agent-vision">支持图像输入</Label><Switch id="agent-vision" className="agent-capability-switch" checked={form.vision} onCheckedChange={checked => patch({ vision: checked })} /></div>
      <div className="settings-form-row"><Label htmlFor="agent-reasoning">支持推理强度</Label><Switch id="agent-reasoning" className="agent-capability-switch" checked={form.reasoning} onCheckedChange={checked => patch({ reasoning: checked })} /></div>
    </> : <div className="settings-form-row"><Label htmlFor="agent-model">模型</Label><FormSelect id="agent-model" value={form.modelId} onValueChange={selectModel} options={[{ value: '', label: '选择模型' }, ...models.map(model => ({ value: model.id, label: model.name }))]} /></div>}
    <div className="settings-form-row"><Label htmlFor="agent-key">API Key</Label><div className="agent-key-controls"><span>{configured ? '已配置' : '未配置'}</span><Input id="agent-key" type="password" autoComplete="off" value={key} placeholder="输入新的 API Key" onChange={event => setKey(event.target.value)} /><Button variant="outline" disabled={!key.trim()} onClick={() => void credential()}>保存密钥</Button><Button variant="ghost" disabled={!configured} onClick={() => void credential(true)}>移除</Button></div></div>
    {!settings.credentialPersistent && <p className="field-hint">当前环境不支持安全存储，API Key 仅在本次应用运行期间保留。</p>}
    <div className="settings-form-row"><Label htmlFor="agent-effort">默认推理强度</Label><FormSelect id="agent-effort" disabled={!form.reasoning} value={form.thinkingLevel} onValueChange={value => patch({ thinkingLevel: value as AgentEffort })} options={['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map(value => ({ value, label: ({ off: '关闭', minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '更高', max: '最高' } as Record<string, string>)[value] }))} /></div>
    <div className="settings-form-row"><Label htmlFor="agent-max-steps">最大工具调用次数</Label><Input id="agent-max-steps" type="number" min={1} max={1000} value={form.maxSteps} onChange={event => patch({ maxSteps: Number(event.target.value) })} /></div>
    <div className="agent-settings-actions"><span role="status">{saved}</span><Button variant="outline" onClick={() => { setForm(settings.configuration); setKey(''); setSaved('') }}>取消</Button><Button disabled={!form.modelId.trim()} onClick={() => void save()}>保存设置</Button></div>
  </fieldset>
}
