import { useState } from 'react'
import { CheckIcon } from '@/components/icons'
import { ArrowLeft, ChevronDown, ChevronRight } from '@/components/icons/workbench'
import { Button } from '@/components/ui/button'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import type { AgentConfiguration, AgentEffort, AgentSettings } from '../../shared/agent'

const efforts: { value: AgentEffort; label: string }[] = [
  { value: 'off', label: 'Off' },
  { value: 'minimal', label: 'Minimal' },
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'xhigh', label: 'XHigh' },
  { value: 'max', label: 'Max' },
]

interface Props {
  settings: AgentSettings | null
  disabled: boolean
  onSettings(): void
  onSelect(configuration: AgentConfiguration): Promise<boolean>
}

export function ModelSelector({ settings, disabled, onSelect, onSettings }: Props) {
  const [open, setOpen] = useState(false)
  const [panel, setPanel] = useState<'summary' | 'models' | 'effort'>('summary')
  const configuration = settings?.configuration
  const currentModel = settings?.models.find(model => model.providerId === configuration?.providerId && model.id === configuration?.modelId)
  const modelName = currentModel?.name || configuration?.modelId || '选择模型'
  const effort = configuration?.reasoning ? configuration.thinkingLevel : 'off'
  const effortLabel = efforts.find(item => item.value === effort)!.label
  const providers = [...(settings?.providers ?? [])].filter(provider => provider.credentialConfigured && provider.modelCount > 0).sort((left, right) =>
    Number(right.id === configuration?.providerId) - Number(left.id === configuration?.providerId))
  const select = async (next: AgentConfiguration) => {
    if (await onSelect(next)) setOpen(false)
  }
  const groups = new Map<string, AgentSettings['models']>()
  for (const model of settings?.models ?? []) {
    const models = groups.get(model.providerId) ?? []
    models.push(model)
    groups.set(model.providerId, models)
  }

  const available = providers.length > 0
  return <Popover open={open} onOpenChange={value => { setOpen(value); if (value) setPanel('summary') }}>
    <PopoverTrigger disabled={disabled || !settings} render={<Button variant="ghost" size="sm" className="composer-model-select" aria-label="选择模型" title={`${modelName} · ${effortLabel}`} />}>
      <span className="composer-model-name">{modelName}</span>
      {configuration?.modelId && <span className="composer-model-effort">{effortLabel}</span>}
      <ChevronDown size={12} className={open ? 'model-selector-chevron-open' : undefined} />
    </PopoverTrigger>
    <PopoverContent side="top" align="end" className="model-selector-popover" aria-label={panel === 'models' ? '选择提供商和模型' : panel === 'effort' ? '选择推理等级' : '模型与推理等级'}>
      {!available ? <div className="model-selector-empty"><p>尚未配置可用模型</p><Button variant="secondary" size="sm" onClick={() => { setOpen(false); onSettings() }}>前往设置</Button></div> : panel === 'summary' ? <div className="model-selector-summary">
        <button type="button" className="model-selector-row" disabled={disabled} onClick={() => setPanel('models')}>
          <span>模型</span><span className="model-selector-value" title={modelName}>{modelName}</span><ChevronRight size={12} />
        </button>
        <button type="button" className="model-selector-row" disabled={disabled || !configuration?.reasoning || !configuration.modelId} title={!configuration?.reasoning ? '当前模型不支持推理等级' : undefined} onClick={() => setPanel('effort')}>
          <span>推理等级</span><span className="model-selector-value">{effortLabel}</span><ChevronRight size={12} />
        </button>
      </div> : <>
        <div className="model-selector-heading"><Button variant="ghost" size="icon-sm" aria-label="返回模型与推理等级" onClick={() => setPanel('summary')}><ArrowLeft size={14} /></Button><span>{panel === 'models' ? '选择模型' : '推理等级'}</span></div>
        {panel === 'models' ? <Command className="model-selector-command" label="搜索模型或提供商" defaultValue={currentModel ? JSON.stringify([currentModel.providerId, currentModel.id]) : undefined} loop filter={(value, search, keywords) => {
          const text = `${value} ${keywords?.join(' ') ?? ''}`.toLowerCase()
          return search.toLowerCase().trim().split(/\s+/).every(term => text.includes(term)) ? 1 : 0
        }}>
          <CommandInput autoFocus placeholder="搜索模型或提供商…" aria-label="搜索模型或提供商" />
          <CommandList>
            <CommandEmpty>没有找到匹配的模型</CommandEmpty>
            {providers.map(provider => <CommandGroup key={provider.id} heading={provider.name}>
              {(groups.get(provider.id) ?? []).map(model => <CommandItem key={model.id} value={JSON.stringify([provider.id, model.id])} keywords={[provider.name, model.name]} disabled={disabled} onSelect={() => {
                if (currentModel === model) { setOpen(false); return }
                if (configuration) void select({ ...configuration, providerId: model.providerId, modelId: model.id, api: model.api, baseUrl: model.baseUrl, contextWindow: model.contextWindow, maxTokens: model.maxTokens, vision: model.vision, reasoning: model.reasoning, custom: false })
              }}>
                <span className="model-selector-name" title={model.name}>{model.name}</span>
                {currentModel === model && <CheckIcon className="model-selector-check" aria-label="当前模型" />}
              </CommandItem>)}
            </CommandGroup>)}
          </CommandList>
        </Command> : <Command className="model-selector-command" label="推理等级" defaultValue={effort} tabIndex={0} ref={element => { element?.focus() }} loop>
          <CommandList>{efforts.map(item => <CommandItem key={item.value} value={item.value} disabled={disabled} onSelect={() => {
            if (effort === item.value) { setOpen(false); return }
            if (configuration) void select({ ...configuration, thinkingLevel: item.value })
          }}><span className="model-selector-name">{item.label}</span>{effort === item.value && <CheckIcon className="model-selector-check" aria-label="当前推理等级" />}</CommandItem>)}</CommandList>
        </Command>}
      </>}
    </PopoverContent>
  </Popover>
}
