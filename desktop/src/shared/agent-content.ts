import type { AgentTodo, AgentToolStatus, AgentToolView } from './agent'

export const toolLabels: Record<string, string> = {
  status: '虚拟机状态', observe: '观察画面', act: '操作虚拟机', wait: '等待画面',
  release_inputs: '释放输入', input_text: '输入文字', clipboard_get: '读取剪贴板',
  clipboard_set: '设置剪贴板', file_open: '打开文件传输', file_read: '读取文件块',
  file_write: '写入文件块', file_close: '关闭文件传输', display_resize: '调整分辨率',
  sync_time: '同步时间', filesystem: '管理文件', powershell: '运行 PowerShell',
  process: '管理进程', registry: '管理注册表', app: '管理应用和窗口',
  snapshot: '读取界面元素', multi_select: '批量选择', multi_edit: '批量填写',
  todo_write: '更新任务进度'
}
export const toolStatusLabels: Record<AgentToolStatus, string> = {
  preparing: '准备参数', running: '执行中', complete: '已完成', error: '执行失败',
  cancelled: '已停止', unknown: '结果未知', ended: '已结束'
}

export function normalizeTodos(value: unknown): AgentTodo[] {
  if (!Array.isArray(value) || value.length > 100) throw new Error('任务列表最多包含 100 项')
  const seen = new Set<string>()
  let active = 0
  return value.map(item => {
    if (!item || typeof item.content !== 'string' || !item.content.trim() || item.content.trim().length > 1000
      || !['pending', 'in_progress', 'completed'].includes(item.status)) throw new Error('任务内容或状态无效')
    const content = item.content.trim()
    if (seen.has(content)) throw new Error('任务内容不能重复')
    seen.add(content)
    if (item.status === 'in_progress' && ++active > 1) throw new Error('最多只能有一个进行中的任务')
    return { content, status: item.status as AgentTodo['status'] }
  })
}

export function todoChanges(previous: AgentTodo[], next: AgentTodo[]): string[] {
  const changes: string[] = []
  const labels = { pending: '待开始', in_progress: '进行中', completed: '已完成' }
  const before = new Map(previous.map((item, index) => [item.content, { ...item, index }]))
  next.forEach((item, index) => {
    const old = before.get(item.content)
    if (!old) changes.push(`新增：${item.content}`)
    else {
      if (old.status !== item.status) changes.push(`${item.content}：${labels[old.status]} → ${labels[item.status]}`)
      if (old.index !== index) changes.push(`顺序变化：${item.content}`)
    }
  })
  const retained = new Set(next.map(item => item.content))
  previous.filter(item => !retained.has(item.content)).forEach(item => changes.push(`删除：${item.content}`))
  return changes
}

export function toolSummary(tool: AgentToolView): string {
  if (tool.todos) {
    const done = tool.todos.filter(item => item.status === 'completed').length
    const current = tool.todos.find(item => item.status === 'in_progress')
    return `更新任务进度 · 已完成 ${done}/${tool.todos.length}${current ? ` · ${current.content}` : ''}`
  }
  return `${toolLabels[tool.name] ?? tool.name} · ${toolStatusLabels[tool.status]}${tool.images.length ? ` · ${tool.images.length} 张截图` : ''}`
}
