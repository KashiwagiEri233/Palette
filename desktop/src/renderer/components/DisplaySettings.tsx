import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type { DisplaySettings, OwnerInfo } from '../../shared/types'
import { DisplaySettingsController } from '../lib/display-settings'

export function DisplaySettingsForm({ owner }: { owner: OwnerInfo | null }) {
  const supported = !!owner?.features.includes('display-settings-v1')
  const controller = useMemo(() => new DisplaySettingsController(async (method, payload) => await window.palette.request(method, undefined, payload) as DisplaySettings), [owner])
  const { settings, busy, error, saved } = useSyncExternalStore(controller.subscribe, controller.snapshot, controller.snapshot)
  useEffect(() => {
    if (supported) void controller.load()
    return () => controller.cancel()
  }, [controller, supported])
  const hint = !owner ? '连接后台后可设置显示模式。' : !supported ? '当前后台不支持此设置，请更新后台并重新连接。' : settings?.configured_mode === 'inherit' ? '当前沿用已有显示配置。开启后使用低延迟模式，关闭后使用兼容模式。' : '关闭后使用兼容模式。'
  return <Card className="display-settings-card"><CardContent className="display-settings-content">
    <div className="display-settings-toggle"><Label htmlFor="low-latency-display">低延迟显示</Label><Switch id="low-latency-display" aria-describedby="display-settings-hint display-settings-activation" checked={settings?.configured_mode === 'low_latency'} disabled={!supported || busy || !settings} onCheckedChange={enabled => void controller.update(enabled)} /></div>
    <p id="display-settings-hint" className="field-hint">{hint}</p>
    <p id="display-settings-activation" className="field-hint">显示模式在关闭虚拟机后重新启动时生效。客户机内重启不会应用此设置。</p>
    {settings?.reason && <p role="status" className="field-hint break-all">{settings.reason}</p>}
    {supported && !settings && busy && <p role="status" className="field-hint">正在读取显示设置…</p>}
    {error && <div role="alert" className="display-settings-error"><p>{error}</p>{!settings && <Button variant="outline" size="sm" disabled={busy} onClick={() => void controller.load()}>重试</Button>}</div>}
    {saved && <p role="status" className="field-hint">显示设置已保存，将在下次启动虚拟机时应用。</p>}
    {!!settings?.instances.length && <div className="display-settings-instances"><Table><TableHeader><TableRow><TableHead>运行中的虚拟机</TableHead><TableHead>当前显示</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>{settings.instances.map(instance => <TableRow key={`${instance.vm_id}:${instance.instance_id}`}><TableCell><span className="break-all">{instance.vm_id}</span></TableCell><TableCell>{instance.provider === 'raw' ? '原始帧' : instance.provider === 'rfb' ? 'VNC' : '未连接'} · {instance.refresh_mode === 'steady' ? '持续刷新' : '自适应刷新'}</TableCell><TableCell>{instance.restart_required ? '待关闭后重新启动' : instance.reason && instance.refresh_mode === 'adaptive' && instance.requested_mode !== 'compatibility' ? '已降级运行' : '已按当前配置运行'}{instance.reason && <p className="field-hint break-all">{instance.reason}</p>}</TableCell></TableRow>)}</TableBody></Table></div>}
  </CardContent></Card>
}
