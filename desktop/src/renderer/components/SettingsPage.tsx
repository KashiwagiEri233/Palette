import { useState } from 'react'
import { FolderIcon } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { FormSelect } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Card, CardContent } from '@/components/ui/card'
import { ButtonGroup } from '@/components/ui/button-group'
import { Kbd } from '@/components/ui/kbd'
import type { OwnerInfo, DoctorReport } from '../../shared/types'

export const settingsSections = [
  { id: 'runtime', label: '运行环境' }, { id: 'agent', label: 'Agent' },
  { id: 'permissions', label: '权限与审批' }, { id: 'appearance', label: '外观' },
  { id: 'shortcuts', label: '快捷键' }, { id: 'about', label: '关于' }
] as const
export type SettingsCategory = typeof settingsSections[number]['id']
export type ThemePreference = 'light' | 'dark' | 'system'

export function SettingsPage({ category, owner, theme, onTheme, onReconnect, onChooseRoot, onError }: { category: SettingsCategory; owner: OwnerInfo | null; theme: ThemePreference; onTheme(theme: ThemePreference): void; onReconnect(): void; onChooseRoot(): void; onError(error: unknown): void }) {
  const [doctor, setDoctor] = useState<DoctorReport | null>(null)
  const [checking, setChecking] = useState(false)
  const modifier = window.palette.platform === 'darwin' ? '⌘' : 'Ctrl'
  return <div className="settings-page page-scroll"><div className="settings-form">
    <header className="page-header"><h1>{category === 'runtime' ? '环境设置' : settingsSections.find(item => item.id === category)?.label}</h1>{category === 'runtime' && <Button variant="outline" disabled={checking || !owner} onClick={() => { setChecking(true); void window.palette.request('doctor').then(result => setDoctor(result as DoctorReport)).catch(onError).finally(() => setChecking(false)) }}>{checking ? '检查中…' : '检查环境'}</Button>}</header>
    {category === 'runtime' && <><Card><CardContent className="settings-content"><div><Label>配置目录</Label><p className="path-value">{owner?.config_root ?? '未连接'}</p><Button variant="secondary" onClick={onChooseRoot}><FolderIcon className="size-4" />选择目录</Button></div><div><Label>运行目录</Label><p className="path-value">{owner?.runtime_root ?? '未连接'}</p></div><div><Label>后台版本</Label><p>{owner?.version ?? '—'}</p><Button variant="outline" onClick={onReconnect}>重新连接</Button></div></CardContent></Card>
      {doctor && <section className="environment-results"><h2>{doctor.ok ? '环境检查通过' : '环境需要配置'}</h2><Table><TableHeader><TableRow><TableHead>检查项</TableHead><TableHead>结果</TableHead><TableHead>详情</TableHead></TableRow></TableHeader><TableBody>{doctor.checks.map((check, index) => <TableRow key={index}><TableCell>{check.name}</TableCell><TableCell>{check.ok ? '通过' : check.blocking ? '需要处理' : '提示'}</TableCell><TableCell className="break-all">{typeof check.detail === 'string' ? check.detail : JSON.stringify(check.detail ?? '')}</TableCell></TableRow>)}</TableBody></Table></section>}</>}
    {category === 'agent' && <><p className="settings-notice">内置 Agent 尚不可用。你可以保存任务草稿，或在工作区接管虚拟机。</p><div className="settings-form-row"><Label htmlFor="agent-provider">模型提供方</Label><FormSelect id="agent-provider" disabled value="unavailable" options={[{ value: 'unavailable', label: '未配置' }]} /></div><div className="settings-form-row"><Label htmlFor="agent-model">模型</Label><FormSelect id="agent-model" disabled value="unavailable" options={[{ value: 'unavailable', label: '暂无可用模型' }]} /></div><div className="settings-form-row"><Label>API Key</Label><span>未配置</span></div><div className="settings-form-row"><Label htmlFor="agent-effort">默认推理强度</Label><FormSelect id="agent-effort" disabled value="unavailable" options={[{ value: 'unavailable', label: '中' }]} /></div><div className="settings-form-row"><Label htmlFor="agent-max-steps">最大步数</Label><Input id="agent-max-steps" type="number" disabled value={100} /></div></>}
    {category === 'permissions' && <><h2>Agent 权限</h2><p className="field-hint">内置 Agent 不可用，当前不会向模型发送画面或执行任务。</p><dl className="permission-list"><div><dt>客户机联网</dt><dd>由虚拟机网络配置决定</dd></div><div><dt>剪贴板同步</dt><dd>关闭</dd></div><div><dt>删除、提交和关机审批</dt><dd>暂不可用</dd></div></dl></>}
    {category === 'appearance' && <><Label>主题</Label><ButtonGroup className="theme-selector" aria-label="主题">{([['light', '浅色'], ['dark', '深色'], ['system', '跟随系统']] as const).map(([value, label]) => <Button key={value} variant={theme === value ? 'default' : 'outline'} aria-pressed={theme === value} onClick={() => onTheme(value)}>{label}</Button>)}</ButtonGroup></>}
    {category === 'shortcuts' && <Table><TableHeader><TableRow><TableHead>操作</TableHead><TableHead>快捷键</TableHead></TableRow></TableHeader><TableBody>{[['新对话', `${modifier}+N`], ['命令面板', `${modifier}+K`], ['切换侧栏', `${modifier}+B`], ['切换工作区', `${modifier}+J`], ['退出全屏', 'Esc'], ['释放人工控制', 'Ctrl+Alt'], ['释放人工控制（兼容）', 'Ctrl+Alt+G']].map(([label, shortcut]) => <TableRow key={label}><TableCell>{label}</TableCell><TableCell><Kbd>{shortcut}</Kbd></TableCell></TableRow>)}</TableBody></Table>}
    {category === 'about' && <><h2>Palette Desktop</h2><p className="field-hint">本地虚拟机工作台</p><dl className="permission-list"><div><dt>后台版本</dt><dd>{owner?.version ?? '未连接'}</dd></div><div><dt>运行平台</dt><dd>{({ win32: 'Windows', darwin: 'macOS', linux: 'Linux' } as Record<string, string>)[window.palette.platform] ?? window.palette.platform}</dd></div></dl></>}
  </div></div>
}
