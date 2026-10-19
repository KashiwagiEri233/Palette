import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog'
import type { GuestAction, GuestStatus } from '../shared/guest'
import type { VmRecord } from '../shared/types'
import { toast } from 'sonner'

export function GuestEnhancements({ vm, status, release, reportError }: { vm: VmRecord; status: GuestStatus | null; release: () => Promise<void>; reportError: (message: string) => void }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [path, setPath] = useState('')
  const [readOnly, setReadOnly] = useState(true)
  const [action, setAction] = useState<GuestAction | null>(null)
  const [bytes, setBytes] = useState(0)
  const commands = status?.qga.commands ?? []
  const fileHandlesAvailable = commands.includes('guest-file-open') && commands.includes('guest-file-close')
  useEffect(() => window.palette.onGuestProgress(event => { if (event.vmId === vm.vm_id) setBytes(event.bytes) }), [vm.vm_id])
  useEffect(() => { setOpen(false); setAction(null); setText(''); setPath('') }, [vm.vm_id, vm.status.instance_id])
  async function run(next: GuestAction): Promise<void> {
    if (!vm.status.instance_id || action) return
    setAction(next); setBytes(0)
    try {
      await release()
      const result = await window.palette.guestAction(vm.vm_id, vm.status.instance_id, next, { text, path, readOnly })
      if (result.completed) toast.success(['text', 'paste'].includes(next) ? '已发送到客户机剪贴板；接管后按 Ctrl+V 粘贴' : '操作完成')
    } catch (error) { reportError(String(error)) }
    finally { setAction(null) }
  }
  const disabled = action !== null || vm.status.state !== 'RUNNING'
  return <Dialog open={open} onOpenChange={value => { setOpen(value); if (value) void release() }}>
    <Button variant="secondary" size="sm" disabled={vm.status.state !== 'RUNNING'} onClick={() => { void release(); setOpen(true) }}>增强工具</Button>
    <DialogContent scrollable className="max-w-xl">
      <DialogHeader><DialogTitle>虚拟机增强工具</DialogTitle><DialogDescription>功能自动检测。服务离线时仍可使用基础画面和键盘，服务恢复后自动启用。</DialogDescription></DialogHeader>
      <div className="space-y-4 text-sm">
        <div><p>{status?.qga.online ? `QEMU Guest Agent ${status.qga.version ?? ''} · 在线` : status?.qga.detail ?? '正在检测客户机服务…'}</p>
          {status?.qga.hostname?.['host-name'] && <p>主机名：{status.qga.hostname['host-name']}</p>}
          {status?.qga.os?.['pretty-name'] && <p>系统：{status.qga.os['pretty-name']}</p>}
          {status?.qga.interfaces?.map(item => <p key={item.name}>{item.name}：{item['ip-addresses']?.map(ip => ip['ip-address']).join('、') || '—'}</p>)}
          <Button className="mt-2" size="sm" variant="outline" disabled={disabled || !commands.includes('guest-set-time')} onClick={() => void run('sync')}>同步客户机时间</Button></div>
        <div className="space-y-2 border-t pt-3"><p className="font-medium">文本剪贴板 {status?.desktop.clipboard ? '· 可用' : '· 尚未就绪'}</p>
          <p className="text-xs text-muted-foreground">需要客户机运行 spice-vdagent。仅在点击时访问宿主剪贴板，单次最多 48 KiB。</p>
          <div className="flex gap-2"><Button size="sm" variant="outline" disabled={disabled || !status?.desktop.clipboard} onClick={() => void run('paste')}>发送宿主剪贴板</Button><Button size="sm" variant="outline" disabled={disabled || !status?.desktop.clipboard} onClick={() => void run('copy')}>复制客户机剪贴板</Button></div>
          <textarea aria-label="发送到客户机的文本" className="w-full rounded-md border bg-transparent p-2" rows={3} maxLength={49152} value={text} onChange={event => setText(event.target.value)} placeholder="中文、多行文本…" />
          <Button size="sm" disabled={disabled || !status?.desktop.clipboard} onClick={() => void run('text')}>发送文本到客户机剪贴板</Button></div>
        <div className="space-y-2 border-t pt-3"><p className="font-medium">文件传输</p><p className="text-xs text-muted-foreground">QEMU Guest Agent 文件接口，单个文件最多 16 MiB。发送会覆盖目标文件；取消后客户机可能保留部分内容。</p>
          <input aria-label="客户机文件绝对路径" className="w-full rounded-md border bg-transparent p-2" value={path} onChange={event => setPath(event.target.value)} placeholder="/home/user/Documents/example.txt" />
          <div className="flex gap-2"><Button size="sm" variant="outline" disabled={disabled || !path || !fileHandlesAvailable || !commands.includes('guest-file-write')} onClick={() => void run('upload')}>发送文件</Button><Button size="sm" variant="outline" disabled={disabled || !path || !fileHandlesAvailable || !commands.includes('guest-file-read')} onClick={() => void run('download')}>取回文件</Button></div></div>
        <div className="space-y-2 border-t pt-3"><p className="font-medium">共享目录 {status?.shares.available ? '· 可用' : '· 客户机服务尚未就绪'}</p><p className="text-xs text-muted-foreground">选择宿主目录后，在客户机文件管理器打开 {status?.shares.url ?? 'dav://127.0.0.1:9843/'}。需要 spice-webdavd；此处选择在虚拟机关机后清除。</p>
          {status?.shares.folders.map(folder => <p key={folder.tag}>{folder.tag} · {folder.read_only ? '只读' : '可读写'}</p>)}
          <label className="flex items-center gap-2"><input type="checkbox" checked={readOnly} onChange={event => setReadOnly(event.target.checked)} disabled={disabled} />只读共享</label>
          <div className="flex gap-2"><Button size="sm" variant="outline" disabled={disabled || !status?.shares.available} onClick={() => void run('share')}>选择共享目录</Button><Button size="sm" variant="outline" disabled={disabled || !status?.shares.folders.length} onClick={() => void run('unshare')}>停止共享</Button></div></div>
        <p className="text-xs text-muted-foreground">{status?.desktop.reason}</p>
        {action && <div role="status" className="flex items-center gap-2"><span>正在处理 · {(bytes / 1024).toFixed(0)} KiB</span><Button size="sm" variant="outline" onClick={() => void window.palette.cancelGuestAction(vm.vm_id)}>取消</Button></div>}
      </div>
    </DialogContent>
  </Dialog>
}
