import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog'
import { useAgentSettings } from '../lib/use-agent'
import { ProviderEditor } from './ProviderEditor'
import type { AgentEffort, AgentProviderView } from '../../shared/agent'

export function AgentSettingsForm({ onError }: { onError(error: unknown): void }) {
  const { settings, reload } = useAgentSettings()
  const [editor, setEditor] = useState<AgentProviderView | 'new' | null>(null)
  const [deleting, setDeleting] = useState<AgentProviderView | null>(null)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState('')
  if (!settings) return <p role="status">正在读取 Agent 设置…</p>
  const changed = async (message: string) => {
    await reload()
    window.dispatchEvent(new Event('palette-agent-settings'))
    setSaved(message)
  }
  const updateDefaults = async (patch: { thinkingLevel?: AgentEffort; maxSteps?: number }) => {
    setBusy(true)
    try { await window.palette.request('agent.settings.update', undefined, { configuration: { ...settings.configuration, ...patch } }); await changed('默认设置已保存') }
    catch (error) { onError(error) } finally { setBusy(false) }
  }
  const removeProvider = async () => {
    if (!deleting) return
    setBusy(true)
    try { await window.palette.request('agent.providers.delete', undefined, { providerId: deleting.id }); setDeleting(null); if (editor !== 'new' && editor?.id === deleting.id) setEditor(null); await changed('提供商已删除，请按需重新选择模型') }
    catch (error) { onError(error) } finally { setBusy(false) }
  }
  const providers = settings.providers.filter(provider => provider.managed)
  return <div className="agent-settings-fields">
    <p className="field-hint">配置模型提供商后，可在聊天输入框中切换模型与推理等级。</p>
    <div className="agent-provider-toolbar"><h2>模型提供商</h2><Button variant="outline" size="sm" disabled={busy} onClick={() => { setEditor('new'); setSaved('') }}>添加提供商</Button></div>
    <div className="agent-provider-list">
      {providers.map(provider => <Card key={provider.id} className="agent-provider-card"><CardContent className="agent-provider-row">
        <div className="agent-provider-identity"><strong>{provider.name}</strong><Badge variant="outline">{provider.custom ? '自定义 API' : '内置'}</Badge><span className="agent-provider-status"><span className={`status-dot ${provider.credentialConfigured ? 'online' : ''}`} />{provider.credentialConfigured ? '已配置' : '未配置'}</span><small>{provider.modelCount} 个模型</small></div>
        <div className="agent-provider-actions"><Button variant="ghost" size="sm" disabled={busy} aria-label={`编辑 ${provider.name}`} onClick={() => { setEditor(provider); setSaved('') }}>编辑</Button>{provider.custom && <Button variant="ghost" size="sm" className="text-destructive" disabled={busy} aria-label={`删除 ${provider.name}`} onClick={() => setDeleting(provider)}>删除</Button>}</div>
      </CardContent></Card>)}
      {!providers.length && <p className="agent-provider-empty">尚未添加提供商。添加内置提供商或自定义 API，即可配置模型。</p>}
    </div>
    {!settings.credentialPersistent && <p className="field-hint">当前环境不支持安全存储，API Key 仅在本次应用运行期间保留。</p>}
    {editor && <ProviderEditor key={editor === 'new' ? 'new' : editor.id} provider={editor === 'new' ? undefined : editor} settings={settings} onCancel={() => setEditor(null)} onSaved={async () => { await changed('提供商已保存'); setEditor(null) }} onError={onError} />}
    <div className="agent-default-settings"><h2>默认设置</h2>
      <div className="settings-form-row"><Label htmlFor="agent-effort">默认推理强度</Label><FormSelect id="agent-effort" disabled={busy} value={settings.configuration.thinkingLevel} onValueChange={value => void updateDefaults({ thinkingLevel: value as AgentEffort })} options={['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map(value => ({ value, label: ({ off: 'Off', minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'XHigh', max: 'Max' } as Record<string, string>)[value] }))} /></div>
      <div className="settings-form-row"><Label htmlFor="agent-max-steps">最大工具调用次数</Label><Input key={settings.configuration.maxSteps} id="agent-max-steps" type="number" min={1} max={1000} disabled={busy} defaultValue={settings.configuration.maxSteps} onBlur={event => { const value = Number(event.currentTarget.value); if (value !== settings.configuration.maxSteps) void updateDefaults({ maxSteps: value }) }} onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur() }} /></div>
    </div>
    <p role="status" className="field-hint">{saved}</p>
    <Dialog open={!!deleting} onOpenChange={open => { if (!open && !busy) setDeleting(null) }}><DialogContent><DialogTitle>删除提供商</DialogTitle><DialogDescription>删除“{deleting?.name}”及其 API Key。若正在使用该提供商，需要重新选择模型；聊天记录与未发送的内容会保留。</DialogDescription><div className="agent-settings-actions"><Button variant="outline" disabled={busy} onClick={() => setDeleting(null)}>取消</Button><Button variant="destructive" disabled={busy} onClick={() => void removeProvider()}>删除提供商</Button></div></DialogContent></Dialog>
  </div>
}
