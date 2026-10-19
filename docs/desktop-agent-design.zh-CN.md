# Palette Desktop GUI 重构与内置 Agent 设计规范

[English](desktop-agent-design.md) | **简体中文**

版本：设计基线 1.0，2026-10-15。交付对象：产品、视觉、React/Electron、Python 后端与测试工程师。本文描述完整目标；内置 pi SDK 已实现聊天、模型凭据、虚拟机视觉工具、上下文压缩与会话分支，实际范围见[桌面指南](desktop.zh-CN.md)。审批、回放和下文完整接口仍为提案。界面文案均为简体中文。

## 0. 设计结论与现有代码边界

Palette 以虚拟机作为工作对象，以会话作为任务历史，以工作区作为实时观察和人工接管入口。会话永久绑定一台虚拟机；切换会话同时切换画面，但不停止后台任务。主页、管理和设置通过全局导航进入；会话视图是主页下的工作路由，主页图标保持选中。

沿用 Electron、electron-vite、React 19、TypeScript、Tailwind v4、本地 Inter、本地图标及 lucide-react。noVNC 始终 `viewOnly=true`，包括人工控制时；键鼠经现有独立输入通道发送。模型请求、凭据、Agent 循环和人工租约心跳由 main 管理；后端共享 RuntimeOwner 仍是控制权与动作有效性的最终裁决者。

已核对的实现入口与必要改动：

| 现状 | 设计落点 |
| --- | --- |
| `desktop/src/renderer/App.tsx` 将管理、创建、详情、退出集中在一起 | 拆成下面的应用壳、路由页面与业务组件；复用现有创建流程 |
| `Console.tsx` 已有 noVNC、InputQueue、坐标换算、全屏与失焦释放 | 提取 DisplaySurface 与输入适配层；租约及心跳移入 main；保留输入顺序和代次校验 |
| `shared/types.ts` 的 request 返回 unknown，main 有方法白名单 | 增量增加逐方法 payload/result 类型及运行时校验；不暴露任意 IPC 或 socket |
| `agent-chat`、`ai-agent-input`、`approval-card` 有内置演示数据、计时器或自动提交 | 在原组件上增加受控模式和插槽，移除业务路径的演示逻辑；不直接挂载默认演示 |
| `ai-agent-input.tsx` 实际导出 `PromptInput`，带模拟增强与硬编码模型 | 复用编辑器和附件布局；改为真实模型能力数据、onSend/onStop，不在 renderer 调用模型 |
| `approval-card` 当前是多问卷，单选可能自动提交 | 新增显式决策模式，复用卡片样式；必须点击“允许”，禁止选择即授权 |
| 桌面 IPC 尚无 vms.pause/vms.resume，也没有 Agent 会话事件流 | 标为新增能力；协商成功前暂停按钮禁用，并提示“当前后台不支持暂停” |
| 现有释放快捷键 Ctrl+Alt+G；窗口最小为 960×640 | 新规范为 Ctrl+Alt，兼容 Ctrl+Alt+G；最小窗口改为 1100×700 |

“推理步骤”只展示模型提供的可公开进度摘要和已执行操作，不展示私有思维链或系统提示词。截图、客户机文本及工具结果均为不可信内容，不授予权限。

## 1. ASCII 线框图

以下图示表达布局关系，尺寸以第 2 节为准。①为全局导航；②为虚拟机和会话；③为聊天；④为工作区。

### 1.1 主页

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ [‹][›][侧栏]   文件  编辑  视图  帮助                        [—][□][×]      │
├────┬──────────────────┬────────────────────────────────────────────────────┤
│ ①  │ ② Palette ▾ [⌕] │                    主页                            │
│ 主 │ [+ 新对话]       │                                                    │
│ 页 │                  │              想让 Agent 做什么？                   │
│    │ ● 开发环境 ▾     │       ┌────────────────────────────────────┐       │
│ 虚 │   整理下载目录   │       │ 描述你的任务…                       │       │
│ 拟 │   安装编辑器     │       │ [+]                 模型 ▾     [↑] │       │
│ 机 │ ○ 测试环境 ▾     │       └────────────────────────────────────┘       │
│    │   暂无对话       │          [● 开发环境 ✓] [○ 测试环境]               │
│ 设 │                  │                                                    │
│ 置 │ 最近             │       运行中的虚拟机                               │
│    │ 整理下载目录     │       [缩略画面 | 开发环境 · 运行中 · CPU/内存 打开]│
│    │   开发环境       │       最近会话                                      │
│    │                  │       整理下载目录 · 开发环境 · 刚刚                │
│ ●  │ 后台已连接       │       [导入磁盘镜像] [从 ISO 安装]                 │
└────┴──────────────────┴────────────────────────────────────────────────────┘
```

主页输入区最大宽 720px；内容列最大宽 960px，上方留白随高度变化 32–96px。目标芯片单选；无虚拟机时发送禁用，下方显示创建入口。资源数据缺失显示“—”，不得将配置内存伪装为实时占用。第一条消息以幂等请求创建会话并提交，成功后进入会话路由；失败保留任务文本、附件和目标。

### 1.2 虚拟机 + 会话三栏视图

```text
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ [‹][›][侧栏]  文件 编辑 视图 帮助                                      [—][□][×]    │
├────┬───────────────────┬─────────────────────────┬───────────────────────────────────┤
│ ①  │ ② Palette ▾ [⌕]  │ ③ 整理下载目录          │ ④ ┌─────────────────────────────┐ │
│ 主 │ [+ 新对话]        │ [● 开发环境]  [⋯][工作区]│   │ 开发环境 运行中 x86_64 [收起]│ │
│ 页 │ ● 开发环境 ▾ [+]⋯│─────────────────────────│   │ [电源组] │ [接管] [⋯]       │ │
│    │   整理下载目录 ◌  │      ┌────────────────┐ │   ├─────────────────────────────┤ │
│ 虚 │   安装编辑器      │      │ 整理下载文件夹 │ │   │                             │ │
│ 拟 │   展开显示        │      └────────────────┘ │   │   ┌─────────────────────┐   │ │
│ 机 │ ○ 测试环境 ▾      │ 用时 2 分 5 秒 ▾        │   │ ‹ │     虚拟机画面      │ › │ │
│    │   暂无对话        │ 执行了 6 个操作 ▾       │   │   │  [✦ 接管任务]      │   │ │
│ 设 │ 最近              │ ┌─────────────────────┐ │   │   └─────────────────────┘   │ │
│ 置 │ 整理下载目录      │ │ 允许删除这些文件吗？│ │   │                             │ │
│    │   开发环境        │ │ 风险说明 / 查看计划 │ │   ├─────────────────────────────┤ │
│    │                  │ │ [不允许]    [允许]  │ │   │1920×1080 · Agent 控制中      │ │
│    │                  │ └─────────────────────┘ │   └─────────────────────────────┘ │
│    │                  │ ┌─────────────────────┐ │                                   │
│ ●  │ 后台已连接        │ │让 Agent 操作这台…   │ │                                   │
│    │                  │ │[+] [开发环境] 模型 ■│ │                                   │
│    │                  │ └─────────────────────┘ │                                   │
└────┴───────────────────┴─────────────────────────┴───────────────────────────────────┘
```

工具栏为一条 44px 高横行，图中分两行仅为说明分组。真实工具栏按容器宽度溢出，不能挤成两行。④可独立隐藏；隐藏后③占满剩余空间，消息正文仍最大宽 760px。

### 1.3 虚拟机管理与详情

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ [‹][›][侧栏] 文件 编辑 视图 帮助                              [—][□][×]    │
├────┬──────────────────┬────────────────────────────────────────────────────┤
│ ①  │ ② Palette ▾ [⌕] │ 虚拟机                               [创建虚拟机]   │
│ 主 │ [+ 新对话]       │ [⌕ 搜索虚拟机…]                                    │
│ 页 │ ● 开发环境 ▾     │ 名称       状态     资源     架构    会话数   操作  │
│ 虚 │   整理下载目录   │ 开发环境   运行中   4核/8G   x86_64   12    [⋯]   │
│ 拟 │ ○ 测试环境 ▾     │ 测试环境   已关机   2核/4G   aarch64   0    [⋯]   │
│ 机 │   暂无对话       │                                                    │
│ 设 │ 最近             │ 点击行后进入： [‹] 开发环境                        │
│ 置 │                  │ [控制台] [状态] [配置]                             │
│ ●  │ 后台已连接       │ 控制台复用④；状态展示实例/控制权；配置展示硬件摘要  │
└────┴──────────────────┴────────────────────────────────────────────────────┘
```

创建向导为模态 Dialog，宽 640px，硬件高级模式最大 800px，高度不超过可用窗口的 85%；正文滚动、页脚固定。三个步骤：选择来源 → 配置 → 确认。复用现有 CreateVm、ResourceInputs、HardwareFields、VmSettings 的校验、能力探测、离开确认及保存逻辑。配置 Tab 为概览，“编辑配置”打开原硬件设置；有停机要求的字段按后端 editable 禁用。

### 1.4 设置

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ [‹][›][侧栏] 文件 编辑 视图 帮助                              [—][□][×]    │
├────┬──────────────────┬────────────────────────────────────────────────────┤
│ ①  │ 设置             │ Agent                                              │
│ 主 │ 运行环境         │ 模型提供方       [选择提供方 ▾]                    │
│ 页 │ Agent         ◀  │ 模型             [选择模型 ▾]                      │
│ 虚 │ 权限与审批       │ API Key          已配置   [更换] [移除]            │
│ 拟 │ 外观             │ 默认推理强度     [中 ▾]                            │
│ 机 │ 快捷键           │ 最大步数         [100]                             │
│ 设 │ 关于             │                                                    │
│ 置 │                  │                                  [取消] [保存]     │
│ ●  │ 后台已连接       │                                                    │
└────┴──────────────────┴────────────────────────────────────────────────────┘
```

设置路由将②替换为分类导航，不额外增加第五栏；离开设置后恢复会话列表宽度、展开状态和滚动位置。表单最大宽 720px，标签区 144px，行距 24px。修改后显式保存；外观立即预览并保存，密钥配置独立提交。切换分类保留未保存内容；离开设置时提示“有未保存的更改”，提供“继续编辑 / 放弃更改 / 保存”。

| 分类 | 字段与行为 |
| --- | --- |
| 运行环境 | 配置目录及[选择目录]、只读运行目录、后台版本及[重新连接]、环境检查表格及[检查环境]；表格列为检查项/结果/详情 |
| Agent | main提供的提供方列表与模型列表、API Key配置状态、默认推理强度、最大步数；默认100步，整数范围1–1000，最终上限以main配置为准；未知模型不可保存 |
| 权限与审批 | 删除/提交/关机/联网/剪贴板分别是否需要审批；允许客户机联网、允许剪贴板默认关闭；关闭权限优先于“不需要审批”；不支持的能力禁用并说明 |
| 外观 | 浅色/深色/跟随系统三选一；预览立即生效并持久化 |
| 快捷键 | 显示宿主平台对应的快捷键表及保留按键说明；第一版只读，不提供未实现的重绑定入口 |
| 关于 | 应用版本、后台版本、许可信息、文档入口；链接经main打开，不显示密钥/端点令牌 |

运行期间更换模型或提供方仅对下一run生效，当前run标题保留实际配置。未配置凭据时，输入框保留可编辑草稿，提示“请先配置模型 [前往设置]”。任务首次启动前展示“任务截图将发送至所选模型服务”，在用户启动动作中明确确认该会话的Agent使用范围；这不等于批准后续高风险操作。

### 1.5 窄窗口与面板折叠

```text
1100–1279px：②自动变成48px虚拟机切换列；③和④并排
┌────────────────────────────────────────────────────────────────────┐
│ 标题栏                                                   [—][□][×] │
├────┬────┬───────────────────────┬──────────────────────────────────┤
│①48 │②48 │③ 标题 [● 开发环境]  │④ 开发环境 运行中 [接管] [⋯]    │
│主页│[+] │  消息流               │  ┌──────────────────────────┐   │
│管理│[A] │                       │  │        实时画面          │   │
│设置│[B] │                       │  │      [✦ 接管任务]       │   │
│ ●  │[⌕] │  [输入框]             │  └──────────────────────────┘   │
└────┴────┴───────────────────────┴──────────────────────────────────┘

有效CSS宽度<1100px（小屏恢复/缩放/测试兜底）：④为右侧模态抽屉
┌────────────────────────────────────────────────────────────────────┐
│ 标题栏                                                    [×]     │
├────┬────┬──────────────────────────────────────────────────────────┤
│①   │②   │③ 对话主区       ║④ 抽屉 [开发环境] [关闭]               │
│    │    │                  ║ [接管] [⋯]                            │
│    │    │ 消息流/遮罩      ║ ┌─────────────────────────────────┐  │
│    │    │                  ║ │             画面                │  │
│    │    │ [显示工作区]     ║ └─────────────────────────────────┘  │
└────┴────┴──────────────────╨──────────────────────────────────────┘

手动收起②：① │③ 对话│④ 工作区；Ctrl+B恢复②
手动收起④：① │② 列表│③ 对话全宽；Ctrl+J恢复④
```

最小原生窗口 1100×700 不取消 `<1100` 兜底：使用 CSS viewport 尺寸决定布局，覆盖缩放、小屏恢复和开发测试。抽屉宽 `min(720px, 100vw - 24px)`；有效宽度不足 544px 时全宽，解除工作区 520px 最小宽，画面继续等比缩放。进入抽屉不会自动打开，标题栏显示“显示工作区”；主动点击缩略图则打开抽屉并定位回放。

## 2. 尺寸、间距、层级与响应式规则

### 2.1 布局表

| 区域/元素 | 默认与范围，CSS px | 间距与层级 | 响应式/溢出 |
| --- | --- | --- | --- |
| 原生窗口 | 首开 1440×900；最小 1100×700 | 100dvh；页面本体不滚动 | 缩放时按 CSS 宽度响应；每块 min-width:0/min-height:0 |
| 标题栏 | 高 48，固定 | z=40；按钮可点击区32；拖动区与 no-drag 控件分离 | Windows 右侧按原生 overlay 实际尺寸避让；macOS 左侧保留80，系统红绿灯代替右侧按钮 |
| ①导航 | 默认48，拖动范围48–64 | 按钮32×32，外边8；图标16；底部状态32 | 所有断点保持；窄窗口固定48 |
| ②列表 | 默认260，最小240，最大280；手动折叠0 | 左右8；分组间16；标题区48；行32；页脚40 | <1280自动48；点击 VM 图标弹240宽会话浮层；手动完全隐藏优先 |
| ③聊天 | 默认420，优选380–460；最小360；最大640 | 标题56；消息左右20；轮次间24；消息内8–12 | <1100最小320；工作区隐藏时可伸展到余宽，正文最大760居中 |
| ④工作区 | 余宽；最小520 | 区域左右/下方合计24预留，卡片radius20，内距12–16 | 极窄抽屉解除最小宽；固定工具栏44、状态栏至少28，其余给画面 |
| 三条分隔条 | 可见线1，命中区8 | z=10；hover显示蓝色线；无布局额外占位 | 分别位于①/②、②/③、③/④；隐藏面板的把手一同移除 |
| 消息流 | 独立 ScrollArea，flex:1 | 用户气泡最大85%，padding12；Agent无底色 | 长代码横向滚动，长链接断行；不撑宽面板 |
| 输入区 | 卡片最小120，文本最高180；整体最高240 | 外边16，内边12，圆角12；下排32 | 高度700时至少保留240消息流；超过正文高度输入框内滚动 |
| 工具/审批 | 工具行28；缩略图96×54；审批全宽 | 审批内16，按钮32高，风险条目间8 | 连续≥3步默认合并；风险与审批打断合并；展开列表分页加载 |
| 画面容器 | 可用空间内 contain；内框radius12 | 画面周边16，最少8；两侧回放按钮32 | 100%时独立双轴滚动，不裁剪客户机内容；适应窗口不修改客户机分辨率 |
| 管理页 | 页边24；表头40、行48 | 搜索宽280；表格无卡片重阴影 | 窄时隐藏次要架构/会话数列到行详情；不隐藏操作 |
| 设置 | 分类导航复用②；表单max720 | 页边24–32；分组间32；字段间24 | 主区<640标签移到控件上方；分类变成页头下拉 |

1440px 基线宽度账本：`48 + 260 + 420 + 3×1 + 24 + 685 = 1440`。1280px 下④为525px，仍满足最小值。用户把①拖到64时优先压缩③到404，再压缩②至240；达到各最小值仍放不下时强制②进入48px形态，不能覆盖画面或出现应用级横向滚动。

①近似固定但其把手允许48–64px小范围调整，以同时满足“约48px”和“三条分隔条可拖拽”。①/②双击恢复①48；②/③恢复②260；③/④恢复③420；④始终取余宽。这里三条把手都调整左侧面板，避免一个双击影响所有列。

采用现有 `resizable` 的 PanelGroup/Panel/PanelResizeHandle 封装；安装版本3使用百分比尺寸，包装层按容器 ResizeObserver 将像素最小值和默认值换算成百分比，不将“520”当百分比传入。固定 gutter 在计算前扣除。拖动时同步计算，结束后持久化。

### 2.2 布局优先级与持久化

1. 画面模式优先级：全屏 > 抽屉 > 用户显式隐藏 > 断点自动形态 > 用户偏好宽度。②显式隐藏始终优先于自动48px列。
2. `<1280` 时②保持48px；Ctrl+B切换“隐藏/48px”，按钮或图标弹出会话浮层。返回≥1280恢复用户上次正常宽度，不把自动折叠写成偏好。
3. `localStorage['palette.layout.v1']` 保存版本、railPx、sidebarPx、chatPx、sidebarHidden、workspaceHidden；设置分类宽度另存。恢复时做 JSON 校验、范围收敛，存储失败回退默认布局。绝不保存凭据、租约、审批或聊天正文到 localStorage。
4. 拖动不卸载 noVNC、不重启 Agent；调整断点仅移动同一个工作区呈现，连接由稳定的 DisplaySessionController 持有。切 VM 才重新绑定显示会话。
5. 分隔条支持方向键每次8px，Shift+方向键32px，Home/End到最小/最大，Enter恢复；aria-valuemin/max/now为当前百分比或说明像素值，名称如“调整对话区宽度”。拖动中关闭动画。

### 2.3 视觉层级与滚动

| 层级 | 内容 | 规则 |
| --- | --- | --- |
| 0–10 | 页面、面板、分隔条 | 面板独立滚动；输入框与工作区工具栏不滚出 |
| 20 | 画面内遮罩、光标、回放条、接管按钮 | 仅占画面，不遮盖工具栏；按钮35%–65%垂直中区居中 |
| 30 | 固定标题/输入区与局部提示 | 不盖过系统窗口控制 |
| 40 | 应用标题栏 | 原生窗口控制命中区独立避让 |
| 50 | 下拉、右键菜单、tooltip | 使用现有 portal；menu关闭返回触发点 |
| 60–70 | 模态抽屉/遮罩、确认 Dialog | Dialog高于抽屉；锁定背景焦点 |
| 80 | Sonner toast | 不遮审批按钮；关键错误也写入持久状态 |

## 3. React 组件树与复用清单

`[复用]`沿用已有业务实现；`[适配]`修改现有 ui 的受控接口；`[新]`新建业务组合组件。业务组件建议放 `renderer/components/{shell,navigation,chat,workspace,settings}`，底层交互仍放 `components/ui`，状态与 IPC hooks 放 `renderer/lib`。

```text
PaletteApp [新，拆分 App.tsx]
├─ AppProviders [新：主题、类型化事件 Store、焦点/快捷键]
├─ WindowTitleBar [新：Button / DropdownMenu / Tooltip / Kbd]
├─ DesktopShell [新：ResizablePanelGroup / ResizablePanel / ResizableHandle]
│  ├─ GlobalNavRail [新：Sidebar + 适配 SidebarNav / Button / Tooltip]
│  │  └─ BackendConnectionIndicator [新：StatusIndicator / Tooltip]
│  ├─ NavigationPane [新：Sidebar / ScrollArea]
│  │  ├─ WorkspaceSwitcher [新：DropdownMenu / Button]
│  │  ├─ NewConversationButton [新：Button / DropdownMenu]
│  │  ├─ VmConversationGroup [新：SidebarGroup / ContextMenu / Badge]
│  │  │  └─ ConversationRow [新：SidebarItem / Tooltip / StatusIndicator]
│  │  ├─ RecentConversations [新：复用 ConversationRow]
│  │  └─ CollapsedVmSwitcher [新：DropdownMenu / Button / Tooltip]
│  └─ RouteOutlet [新]
│     ├─ HomePage [新：EmptyState / ScrollArea]
│     │  ├─ HomeComposer [新：复用 AgentComposer]
│     │  ├─ TargetVmChips [新：ButtonGroup / Badge]
│     │  ├─ RunningVmCard [新：Button / Badge / StatusIndicator]
│     │  └─ QuickCreateActions [新：Button，接现有创建向导]
│     ├─ ConversationPage [新：ResizablePanel / ResizableHandle]
│     │  ├─ AgentChatPanel [适配 agent-chat：受控布局，移除演示轮次]
│     │  │  ├─ ConversationHeader [新：Badge / DropdownMenu / Button / Tooltip]
│     │  │  ├─ MessageTimeline [新：ScrollArea / EmptyState]
│     │  │  │  └─ MessageItem [新：角色分支，安全 Markdown 与代码渲染]
│     │  │  │     ├─ RunProgress [新：ThinkingSteps / ThinkingIndicator]
│     │  │  │     ├─ ToolStepGroup [新：ToolChips / Button]
│     │  │  │     │  └─ ToolStepRow [新：Button / Tooltip，截图缩略图]
│     │  │  │     ├─ AgentApprovalCard [适配 ApprovalCard 决策模式 / Button]
│     │  │  │     ├─ MessageActions [新：Button / Tooltip]
│     │  │  │     └─ RunResultCard [新：Badge / Button]
│     │  │  └─ AgentComposer [适配 ai-agent-input 的 PromptInput]
│     │  │     ├─ VmAvailabilityBanner [新：Button / StatusIndicator]
│     │  │     └─ ModelControls [新：DropdownMenu / Button / Tooltip]
│     │  └─ VmWorkspace [新：下列组件 + 同一呈现的 Dialog 抽屉模式]
│     │     ├─ VmToolbar [新：ButtonGroup / DropdownMenu / Badge / Tooltip]
│     │     ├─ ReplayBanner [新：Button]
│     │     ├─ DisplaySurface [复用并提取 Console.tsx，noVNC只读]
│     │     │  ├─ DisplayStateView [新：EmptyState / Button / 骨架样式]
│     │     │  ├─ AgentControlOverlay [新：Button / StatusIndicator]
│     │     │  ├─ HumanControlLabel [新：Badge / Kbd]
│     │     │  └─ ReplayControls [新：Button / Tooltip]
│     │     └─ WorkspaceStatusBar [新：StatusIndicator / Tooltip]
│     ├─ VmManagementPage [新：Input / Table / Button / EmptyState]
│     ├─ VmDetailPage [新：Tabs + VmWorkspace / LiveStatus / Configuration]
│     └─ SettingsPage [新：Sidebar / ScrollArea / Label / Input]
│        ├─ RuntimeSettings [复用现有环境页 / Table / Button]
│        ├─ AgentSettingsForm [新：NativeSelect / Input / Button]
│        ├─ PermissionSettingsForm [新：Checkbox / Button / Dialog]
│        ├─ AppearanceSettings [新：ButtonGroup]
│        ├─ ShortcutSettings [新：Kbd / Table]
│        └─ AboutSettings [新：Button]
├─ CreateVmWizard [复用 CreateVm / Dialog / HardwareFields / ResourceInputs]
├─ VmSettingsDialog [复用 VmSettings]
├─ CommandPalette [新：Command / Dialog]
├─ ConversationSearch [新：Command / Dialog，按vm和会话分组]
├─ ConfirmActionDialog [新：Dialog / Button，覆盖危险操作和清空/删除]
├─ ExitDialog [复用现有 Dialog，新增运行任务摘要]
└─ ToastHost [复用 Sonner]
```

没有引入第二套侧栏、输入框、审批卡或拖拽库。SidebarNav 同样需要用真实导航替换默认演示条目，关闭原示例业务状态。优先从 `components/icons` 取图标，缺失时用 lucide-react；禁止运行时下载图标或字体。Markdown 渲染选型在实现阶段确定，必须禁用原始 HTML、脚本 URL 和远程自动加载图片，外链通过 main 白名单打开。

## 4. 状态清单与可用性

状态是正交维度，不能压成单一 `isBusy`：后台连接、VM生命周期、AgentRun、控制权、画面连接、回放各自建模。例如 Agent 等审批时仍可实时看画面；历史回放时 Agent 可能继续在后台运行。

下表覆盖上面所有业务组件族；叶子组件继承对应行，纯容器继承页面状态。`—`表示该维度不适用；禁用项仍可由 tooltip/可读说明解释原因。

| 组件族 | 空 | 加载 | 错误 | 禁用 | Agent运行 | 人工接管 | 等待审批 | 回放 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| PaletteApp/AppProviders/DesktopShell/RouteOutlet | 默认主页 | 连接骨架 | 持久错误条+重连 | 退出中锁变更 | 后台运行不中断 | 绑定当前控制视图 | 汇总待办数 | 保持独立回放选择 |
| WindowTitleBar/GlobalNavRail/BackendConnectionIndicator | 默认主页 | 连接点+文字 | 灰点+断线详情 | 无历史时后退前进 | 导航可用 | 切路由先释放 | 状态可见 | 返回不取消任务 |
| NavigationPane/WorkspaceSwitcher/CollapsedVmSwitcher | “还没有虚拟机”+创建 | 分组骨架 | 内联重试 | 断线禁写 | 当前会话转圈 | 鼠标图标 | 蓝点+“等待审批” | 选择仍指会话 |
| NewConversationButton | 无VM打开选择/创建 | 创建中 | 文本保留+重试 | 同请求处理中 | 可建会话 | 不自动转移控制 | 不替换旧审批 | 新会话回实时 |
| VmConversationGroup/ConversationRow/RecentConversations | “暂无对话” | 32高骨架 | 分组重试 | 归档只读；关机可选 | 转圈 | 鼠标 | 强调点+文字 | 缩略图指向原会话 |
| HomePage/TargetVmChips/QuickCreateActions | 创建入口 | VM卡骨架 | 连接失败条 | 无目标禁发送 | 目标占用显示“查看” | 当前VM显示接管 | 最近会话显示待审批 | — |
| RunningVmCard | 无运行中VM说明 | 缩略骨架 | 缩略失败占位 | 开启中防连点 | 状态徽标 | 状态文字 | 待审批徽标 | 打开进入实时 |
| AgentChatPanel/ConversationHeader | 标题“新对话” | 标题骨架 | 历史加载失败重试 | 删除中/标题提交中 | 标题显示执行中 | “人工控制中” | “等待审批” | 工作区开关保留 |
| MessageTimeline/MessageItem | 任务建议，无示例消息 | 历史骨架/流式文本 | 局部失败卡 | 已归档不可编辑 | 流式增量 | 插入系统消息 | 卡片定位入口 | 选中对应步骤 |
| RunProgress | 无摘要不渲染 | “正在观察画面…” | “执行中断” | — | 折叠用时/步骤 | 计时暂停 | 暂停执行计时 | 展开不改执行 |
| ToolStepGroup/ToolStepRow | 无步骤不渲染 | 当前步转圈 | 失败/结果未知 | 缺截图禁缩略预览 | ≥3合并 | “已暂停” | 阻塞步不执行 | 高亮选中步 |
| AgentApprovalCard | 无审批不渲染 | 决策提交中 | 留卡+错误重试 | 已决策/过期/失配 | 等待时停止动作 | 旧审批作废 | 标题/风险/计划/两按钮 | 审批仅对实时上下文有效 |
| MessageActions/RunResultCard | 无结果不渲染 | 保存反馈中 | 可重试反馈 | 运行中禁重试 | 可复制已生成内容 | 统计接管次数 | 无完成卡 | 可打开最终截图 |
| AgentComposer/HomeComposer/ModelControls | 占位文本 | 提交中 | 草稿保留 | 未运行/占用/无凭据/归档 | 发送变停止；可写草稿 | “交还 Agent”入口 | 停止可用，发送禁用 | 草稿可写；发送先回实时 |
| VmAvailabilityBanner | 未选VM提示选择 | 启动中 | 启动失败可重试 | 启停中禁启动 | 占用显示查看 | 释放失败提示 | 审批定位 | “正在查看历史画面” |
| VmWorkspace/VmToolbar | 未选择VM | 生命周期忙 | 保留错误及日志入口 | 按下表 | 接管可用 | 交还可用 | 接管仍可用 | 危险操作先回实时 |
| ReplayBanner/ReplayControls | 无截图隐藏 | 截图骨架 | 重试/截图已移除 | 首尾禁前后 | 注明“实时任务仍在执行” | 进入先释放人工 | 卡片可阅读 | 第n/N步+实时按钮 |
| DisplaySurface/DisplayStateView | 已关机+启动 | 启动骨架/连接中 | 错误码+日志+重启 | 只读/输入封锁 | 轻遮罩+点击波纹 | 蓝边+释放标签 | 画面只读 | 静态截图，禁止输入 |
| AgentControlOverlay/HumanControlLabel | — | “正在接管…” | 接管失败重试 | 转移中防连点 | 蓝色接管药丸 | 人工标签 | 接管会撤销旧审批 | 回放不显示实时点击波纹 |
| WorkspaceStatusBar | 分辨率“—” | “正在连接” | 持久控制故障说明 | — | Agent控制中 | 人工控制 | 仅查看·等待审批 | “历史画面·仅查看” |
| VmManagementPage/VmDetailPage/LiveStatus/Configuration | 创建入口/无匹配 | 表格/详情骨架 | 局部重试 | 变更中锁该VM | 任务归属可点击 | 控制归属可见 | 待审批会话链接 | 控制台共享回放状态 |
| SettingsPage及六类表单 | 未配置状态 | 读取配置 | 字段错误+提交失败 | 无能力/保存中 | 影响当前任务的改动延后 | 网络/根目录变更先释放 | 权限变更使旧审批失效 | — |
| CreateVmWizard/VmSettingsDialog | 初始表单 | 探测/保存中 | 定位字段+保留输入 | 能力缺失/运行时不可改 | 硬件修改须先停止任务 | 修改前释放 | 不能隐式授权变更 | — |
| CommandPalette/ConversationSearch | 无结果 | 本地索引加载 | 索引失败提示 | 动作按状态禁用 | 提供停止/定位 | 提供释放 | 提供跳到审批 | 提供回实时 |
| ConfirmActionDialog/ExitDialog | — | 正在提交，防重复 | 保留Dialog与错误 | 提交中禁再次确认 | 列出运行任务 | 退出前释放 | 完全退出撤销审批 | 关闭不改历史 |
| ToastHost | 不渲染 | — | 可读结果 | — | 仅关键变化 | 自动释放原因 | 不替代审批卡 | 不覆盖画面中部 |

### 4.1 VM生命周期与工具栏可用性

| 状态 | 启动 | 关机 | 重启 | 暂停/继续 | 更多危险操作 | 画面与输入 |
| --- | --- | --- | --- | --- | --- | --- |
| 已关机 STOPPED | 启用 | 禁用 | 禁用 | 禁用 | 恢复初始状态/删除可用 | 空状态；Agent与人工输入禁用 |
| 启动中 STARTING | 禁用 | 禁用 | 禁用 | 禁用 | 强制停止可用 | 骨架+“正在启动虚拟机…”；不伪造百分比 |
| 运行中 RUNNING | 禁用 | 启用 | 持久VM启用 | 支持时“暂停” | 强制停止可用；恢复/删除禁用 | 连接就绪且控制有效才可输入 |
| 关机中 STOPPING | 禁用 | 禁用 | 禁用 | 禁用 | 强制停止可用，冲突变更中禁重复 | 最后画面+“正在关机…”；封锁输入 |
| 已暂停 PAUSED | 禁用 | 后端支持时启用 | 后端支持且持久VM时启用 | 支持时“继续” | 强制停止可用；恢复/删除禁用 | 冻结画面+“虚拟机已暂停”；Agent暂停 |
| 故障 FAILED | 后端确认无活动实例时启用 | 禁用 | 后端报告可重启时启用 | 禁用 | 仍有进程可强停；无进程才可恢复/删除 | 错误码/信息/[查看日志]/[重启]，不可用则改[启动] |

更多危险项固定显示红色文字和危险图标，禁用时说明原因。非持久VM按现有后端能力收敛。强停、恢复、删除使用二次确认，文字必须带VM名称与影响；恢复明确“丢弃当前磁盘修改”，删除明确“删除定义和受管理磁盘，原始镜像不会被删除”。删除VM同时说明将删除其全部会话与截图；禁止产生孤立 vm_id。Agent活跃时先停止并确认输入释放，再执行生命周期动作。用户从工具栏主动点击无需再重复弹Agent审批卡。

工具栏容器宽≥960时完整呈现；720–959时快捷键、缩放、重连、设置移入“⋯”；520–719只保留身份、启动/关机主动作、接管/交还、全屏与“⋯”。名称最长160px、紧凑模式96px截断；架构最先转移到身份tooltip。Tab顺序与视觉顺序一致。Ctrl+Alt+Del、Super仅在人工控制且实时画面时启用，不能调用 RFB.sendKey 绕过控制通道。

### 4.2 列表、聊天与回放细则

- 分组按置顶VM（若后续提供）/名称排序；当前版本无VM置顶时只按名称。会话先置顶、再按 updatedAt 倒序；每组默认展示5条，“展开显示（N）”可逆。已关机分组只降低图标和装饰对比，不给整个文本容器统一 opacity，以保持文字对比度。
- 最近分区包含所有未归档会话，不去掉已出现在分组中的条目；两处引用同一ID，高亮同步。标题单行截断，tooltip显示完整标题；VM副标题12px。组头操作在hover及focus-within出现。
- 会话右键提供重命名、置顶/取消置顶、归档、删除；VM右键提供状态允许的启动、关机、打开设置、在管理页查看。归档运行中的会话先停止并确认，归档不删除磁盘；清空、删除先停止且需要二次确认。清空保留会话ID和VM绑定，删除移除会话及其受管媒体。搜索默认不含归档，可切换“包含已归档”。
- 标题点击进入行内编辑，Enter保存、Esc取消，空标题不提交；正在编辑时外部事件不覆盖本地未提交值。搜索用Command，按“虚拟机/会话”分组，结果显示所属VM。
- 新消息仅当用户距底部≤80px时自动滚动；否则显示“有新消息 ↓”。切会话恢复各自滚动位置、草稿、展开步骤和回放位置。切换不取得控制权；人工控制跨VM/离开控制台时先释放。
- 用时只累计实际执行时间，排除审批和人工接管等待；详情另列总耗时。工具摘要不直接输出敏感输入原文，密码等显示“输入了受保护文本”。点赞/点踩为本地反馈，不自动发送给第三方。
- 重试创建新的run，不重放旧键鼠操作；从最新截图重新观察。历史回复与失败步骤保留；结果未知时必须先核对画面。完成卡“完成 · 共14步 · 人工接管1次”；失败/取消分别显示“任务未完成/已停止”，不得伪装成功。
- 截图缩略图点击选中 stepId：优先after、其次before，标明拍摄时间、分辨率及“操作前/后”；无截图则不可选。前后按钮只遍历有截图的步骤，N表示该run可回放步数。原始步骤序号同时展示，例如“回放5/12 · 操作7”。
- 回放是浏览状态，不停止Agent；顶部明确“任务仍在执行”。回放输入完全禁用；点击接管先返回实时并取新快照，再走接管协议。正在人工控制时进入回放必须先释放，不能在历史图上发送坐标。100%缩放的坐标映射扣除滚动偏移与留白；旧displayEpoch点击被拒绝。

## 5. 关键交互与时序

### 5.1 主流程

```text
用户          Renderer/Preload           Main AgentCoordinator        Shared daemon
 │ 选VM/发送       │                             │                         │
 ├────────────────>│ conversations.create(vm_id) │                         │
 │                 ├────────────────────────────>│ 持久化会话/消息         │
 │                 │ agent.start(requestId)      │                         │
 │                 ├────────────────────────────>│ 原子认领每VM驱动会话    │
 │                 │                             ├──状态/控制权检查────────>│
 │                 │                             ├──observe截图────────────>│
 │                 │<──run/step/message事件───────┤<──Frame及epochs──────────┤
 │                 │                             │ 模型决定下一步          │
 │                 │                             │ 审批策略检查            │
 │<──审批卡────────┤<──approval.pending───────────┤ 暂停动作调度            │
 │ 点击允许        │                             │                         │
 ├────────────────>├──approvals.decide───────────>│ 核对run/摘要/策略/epochs │
 │                 │                             ├──有界动作──────────────>│
 │                 │<──步骤/截图事件─────────────┤<──动作回执──────────────┤
 │ 点击接管        │                             │                         │
 ├────────────────>├──control.takeover───────────>│ 封锁下一步/取消待执行    │
 │                 │                             ├──control.acquire────────>│
 │                 │                             │<──旧输入释放+新代次──────┤
 │                 │<──human/control事件─────────┤ main持有人工租约及心跳   │
 │<──你已接管控制──┤                             │                         │
 │ 键鼠操作        ├──control.input──────────────>├──租约+顺序+坐标校验─────>│
 │ 点击交还Agent   │                             │                         │
 ├────────────────>├──control.return─────────────>├──control.release────────>│
 │                 │                             │<──release_confirmed──────┤
 │                 │                             ├──重新observe────────────>│
 │                 │<──resuming/observing─────────┤<──当前帧────────────────┤
 │                 │                             │ 重新规划/必要时重新审批 │
 │                 │<──completed+结果摘要────────┤                         │
```

1. **新建会话**：必须有vm_id；关机VM可以建会话并保存草稿，不能启动run。点击提示条“启动”，等待RUNNING、画面就绪后由用户再次发送，避免在不知情时自动执行旧文本。同一请求重试不产生重复会话或重复消息。
2. **Agent执行**：main先持久化用户消息并认领VM，后台核对人类租约和已有Agent动作；每步是observe → 规划 → 审批检查 → act → 回执 → observe。一个VM只有一个桌面驱动run；不同VM可以并行，受全局并发上限约束。
3. **审批**：默认删除、提交、关机、联网、剪贴板按策略阻塞。卡片说明对象、影响、一次授权范围；“查看计划”是可公开动作计划。“不允许”取消该动作并让run进入paused(approval_denied)，提供“调整任务/停止”，不能绕过或自动重试相同风险。允许仅对匹配actionDigest的一次动作有效，执行前重新校验，后台策略仍可拒绝。
4. **接管**：main在每VM串行协调锁内置taking_over，先停新动作、取消排队动作/模型请求，再由后端acquire推进controllerEpoch并释放保持键鼠。旧动作仅允许已不可取消的当前有界事务收尾；接管按钮显示“正在接管…”，收到租约且release_confirmed后才允许人工输入。失败则只读、显示错误；不得先画人工描边再假定成功。成功后系统消息“你已接管控制”，接管计数+1，旧待审批请求expired。
5. **交还**：先封锁/排空本地InputQueue并释放所有按键、按钮，再release租约。只有后端确认释放后才切只读、重新observe；旧坐标、帧、计划和授权全部作废。run仍可恢复且用户选择继续时才重新规划；无可恢复run时按钮显示“结束控制”。
6. **完成**：以main终态事件为准，汇总已执行步数、接管次数及结果；清理占用并持久化消息。迟到的旧run增量不能将终态改回运行中。下一条消息创建新的run。

### 5.2 必须明确的异常分支

| 触发 | Main/后端动作 | 界面与恢复 |
| --- | --- | --- |
| 人工控制时窗口失焦/隐藏、全屏退出、心跳失败 | 立即封锁输入，停队列，释放；main自己监听窗口事件，不能只依赖renderer | toast“窗口失去焦点，已释放控制”；保持Agent暂停，需要明确“继续任务” |
| release未确认/后端断开 | ControlState=faulted，保留未知状态，不启动Agent；等待后端租约失效后重新查询确认 | 持久提示“控制释放尚未确认，已暂停输入”+[重试连接]；不能只toast |
| 画面断连/分辨率变化/实例重启 | 停止Agent写入、释放人工；旧frame和审批失效 | 显示重连/已变更；重新observe后才能恢复；不靠最后一帧继续盲点 |
| 同VM另一会话发送 | main原子拒绝VM_BUSY，返回ownerConversationId | “另一会话正在操作此虚拟机 [查看]”；切换不停止原会话 |
| 外部MCP也在操作 | 桌面锁不足以保护跨客户端，沿用共享后端串行动作/代次/控制策略；新增驱动声明需在RuntimeOwner实现 | 不宣称仅main锁就排他；本次上线必须覆盖外部客户端竞争测试 |
| 用户停止/Esc | 取消模型与后续步骤，释放保持输入，当前不可逆动作可能已完成；unknown则先observe核对 | “正在停止…”→“已停止”；再次Esc不重复派发；停止不关VM |
| 关闭窗口 | 仍显示仅关闭/完全退出/取消；执行退出前锁新run | 有N个任务时追加“仅关闭窗口后任务将暂停”；停Agent并持久化，释放人工，后台VM继续运行 |
| 完全退出 | 先停止所有run、取消审批、确认释放，再沿用RuntimeShutdown正常关VM/停当前后台 | 不成功保留退出Dialog与错误；MCP托管场景保留现有中断相关连接说明 |
| main重启/应用崩溃恢复 | 恢复历史，把非终态run标记interrupted，查询后端后才允许新写入 | “上次任务已中断 [从当前画面继续]”；不自动恢复旧动作或审批 |
| 删除/清空会话或删除VM | 停run→释放→事务删除记录及受管媒体引用；磁盘操作仍在原后端 | 失败保留对象和错误；VM删除成功后导航到主页，绝不自动绑定另一VM |

### 5.3 键盘与无障碍

| 快捷键 | 行为 |
| --- | --- |
| Ctrl+N / macOS Cmd+N | 当前VM新对话；未选VM则打开选择器 |
| Ctrl+K / Cmd+K | Command命令面板，支持VM/会话查找及当前状态可用动作 |
| Ctrl+B / Cmd+B | 显示/隐藏②；设置页为分类导航 |
| Ctrl+J / Cmd+J | 显示/隐藏④，抽屉模式为打开/关闭 |
| Enter / Shift+Enter | 发送 / 换行；输入法isComposing期间不发送 |
| Ctrl+Alt | 人工控制时释放；只在这两个修饰键单独按下再释放且未出现其他键时触发 |
| Ctrl+Alt+G | 兼容原释放方式；不转发到VM |
| Esc | 优先关闭最上层菜单/Dialog/抽屉，再退出画面全屏，再停止当前会话Agent；一次只消费一个动作 |

保留的宿主快捷键在画面聚焦时也不能发送到客户机；Ctrl+Alt+Del经工具栏显式输入，不被“Ctrl+Alt释放”误拦。用户点击Toolbar或输入框时退出画面键盘捕获；释放不能由重新focus自动恢复。全屏只呈现工作区，保留固定退出/接管入口。

所有图标按钮有中文aria-label、tooltip，命中区至少32px；导航aria-current，折叠aria-expanded，状态点附文字/图标。画面区域 `role="region"`、tabIndex=0，名称随状态变化，如“开发环境虚拟机画面，人工控制中，按Ctrl+Alt释放”。不使用会捕获全部读屏按键的application角色。消息流用log与受控的polite通知，仅播报轮次/审批/完成摘要，避免每个文本token或鼠标移动都朗读。审批出现不强抢焦点，提供“跳到待审批”；用户打开时先聚焦标题/风险，不默认聚焦“允许”。

模态Dialog和抽屉锁定焦点，关闭后回原触发点；被删除时回最近可用会话行。标题栏拖动区不覆盖交互控件。文字对比至少4.5:1，非文本焦点/关键边界至少3:1；浅装饰边框不承担唯一识别。全流程可仅键盘完成，客户机屏幕内容本身不声明可读屏访问。

## 6. 渲染数据模型、状态存储与 IPC 草案

完整可类型检查的模型和方法映射在 [desktop-agent-contract.ts](desktop-agent-contract.ts)。它是本规范配套草案，未接入应用编译，不是已实现API。

### 6.1 数据归属

| 实体 | 关键字段/约束 | 持久化与归属 |
| --- | --- | --- |
| Conversation | id、必填vm_id、title、pinned、archivedAt、revision、activeRunId、draft；vm_id创建后不可变 | main会话仓库；renderer只持缓存 |
| Message | conversationId、vm_id、runId、role、status、content、附件引用、公开进度、toolStepIds、approvalIds、结果 | main追加/更新；角色和run关系由main生成，不接受renderer伪造assistant |
| ToolStep | runId、index、kind、summary、status、before/after、point、actionSeq、error | main记录真实回执；无截图时明确null；坐标属于截图像素空间 |
| ApprovalRequest | run/step/vm、actionDigest、epochs、policyRevision、expiresAt、status、single_action | main持有且后端策略复核；renderer只提交allow/deny及期望版本 |
| AgentRun | state、modelId、reasoningEffort、maxSteps、stepCount、takeoverCount、revision | main拥有调度；终态不可被迟到事件复活 |
| ControlView | state、ownerConversationId、epochs、releaseConfirmed | 后端事实经main投影；lease_id仅main持有 |
| VmAgentView | 原VmRecord适配的状态/架构/连接/分辨率、控制视图、活跃run、可空指标、feature能力 | 原后端为事实来源；不将control_owner=agent等价于Agent正在执行 |
| ScreenshotRef/AttachmentRef | ID、尺寸/MIME、拍摄时间、epochs；不暴露任意本机路径 | main受管目录与授权媒体协议；截屏和附件有删除/保留策略 |
| AgentSettings | 提供方、模型目录、支持的推理强度、步数、credentialConfigured、权限revision | main持久化；模型能力不硬编码；不支持推理强度时隐藏选项 |

main仓库存储在app.getPath('userData')下的会话目录，与VM根目录ID建立索引；不用renderer localStorage存正文。写入使用事务或原子替换，崩溃后可恢复。截图默认保留最近30天，可在设置清理；会话文本保留到删除，截图到期后展示“截图已清理”。导出经main原生保存Dialog，默认排除受保护输入和媒体原始路径。切换配置根目录先停止任务并释放，历史按根目录隔离，不能把旧vm_id指向新根目录的同名VM。

renderer规范化Store按 `conversations/messages/runs/steps/approvals/vms` 的ID索引保存；UI局部状态单独存 selectedConversationId、selectedStepId、viewMode、scrollAnchors、draftEdits。事件reducer不调用键鼠和模型；所有业务动作显式走request。

### 6.2 类型化业务 API

保持 `window.palette.request(method, vmId, payload)` 入口；逐方法映射payload和result，禁止任意字符串。新增接口先由main路由，只有真实后端方法进入原Operator IPC白名单；不得将 `conversations.*`/`agent.*` 盲传Python。

| 方法组 | 作用与必须校验 |
| --- | --- |
| agent.bootstrap | 原子快照+streamEpoch/cursor；只带选中会话的一页历史 |
| conversations.create/list/history/update | vm_id有效；revision冲突不覆盖；分页；重命名/置顶/归档/草稿共用patch |
| conversations.clear/delete/export | 停止任务与引用清理；导出通过原生保存框，不接受任意目标路径 |
| messages.feedback | 本地反馈且校验消息归属 |
| agent.start/stop/resume | requestId去重、预期instance、每VM互斥、状态/模型/权限检查；resume重新observe |
| approvals.decide | run仍等待、单次摘要/版本/epochs/策略/期限一致；重复点击返回原决定 |
| control.takeover/return/release/input | main持有租约；renderer仅提供输入事件及controlRevision、seq和几何；失配一律拒绝 |
| screenshots.get / attachments.choose | 受管ID解析、会话归属、MIME/大小校验；原生文件选择在main，当前截图由后端observe获取 |
| agent.settings.get/update / permissions.update | 不返回密钥；权限收紧立即生效并使旧审批过期；放宽仅影响新run，当前run使用更严格策略 |
| credentials.configure | 仅providerId和操作模式，不传secret字段；仅返回configured布尔 |
| window.command | 受限枚举、校验发送窗口身份；close仍进入退出确认 |
| vms.pause/resume | 新增后端能力；需实现运行状态转换、输入释放和frame失效后才启用 |

返回 `Result<T>`，错误包含code、中文message、retryable及必要的ownerConversationId，不回传原始堆栈、令牌、模型请求头。每个变更带requestId；main持久化去重键及结果到恢复周期，客户端对超时查询状态，禁止自动重发结果未知的动作。

旧 `control.acquire/heartbeat/release` 在main的后端适配器继续使用。迁移完成后renderer不再拿lease_id；`control.release/input`同名接口的新renderer载荷由main转换为旧后端协议，不直接透传。main每秒续约，后端当前3秒租约到期为最后保障；释放确认失败仍然封锁输入。

### 6.3 事件流订阅与恢复

preload新增唯一的类型化订阅 `subscribeAgent(options, listener)`，不暴露ipcRenderer或通道名。options含客户端生成的subscriptionId、streamEpoch、afterSeq；返回ready Promise与unsubscribe。preload先安装固定通道监听，再invoke注册；main原子完成历史补发和实时挂接，所以bootstrap与订阅之间不会丢事件。订阅只向已校验的webContents发送；不能跨配置根目录复用。

事件种类：conversation.upsert/deleted、message.upsert/delta、step.upsert、approval.upsert、run.upsert、control.changed、vm.upsert、settings.changed、stream.reset_required。每个事件带subscriptionId、streamEpoch、全局递增seq和emittedAt，实体带revision。示例订阅见配套TS文件的mountAgentStore；实际React effect还需处理异步bootstrap取消，在卸载后返回时立即unsubscribe。

reducer规则：

1. 单应用根级订阅，切会话不反复建监听。按配置根目录的完整流递增seq，不做会产生缺号的静默过滤；旧subscriptionId/streamEpoch丢弃。
2. `seq<=lastSeq`去重；`seq>lastSeq+1`暂停增量归并并请求补发；delta必须匹配messageId、revision和字符offset，否则拉完整message，不拼接重复文字。先暂存乱序事件，队列有上限。
3. main至少保留最近10分钟或10,000条事件（任一上限触发截断）；cursor过期、epoch变化或队列溢出则发reset_required，重新bootstrap，再订阅。不把历史动作重新执行。
4. 一次发送的IPC消息上限64KiB；文本delta每30–50ms合并，不能合并掉审批/控制/终态事件。图片不走事件base64；只带引用。超长正文通过历史分页/受限分块恢复。
5. 卸载、重连、开发StrictMode重复mount时清理监听；unsubscribe幂等，监听器先摘除再注销订阅。renderer重载后重建快照，不重启run。

补发通过从lastSeq重新注册订阅完成，替换旧订阅；不能同时保留两个活跃监听。清空后main发送conversation.upsert并携带historyReset=true，renderer据此使历史缓存失效并重新history；普通重命名的revision更新不清空历史。delete事件清除该会话的消息、步骤、审批和回放选择。

### 6.4 进程边界与安全实现条件

```text
Renderer: 页面、消息缓存、只读noVNC、用户输入意图
    ↓ contextBridge typed request / subscribeAgent
Preload: 固定IPC通道、类型封装、事件校验与清理
    ↓ 受信窗口身份校验、payload schema、限流、requestId去重
Main: AgentCoordinator、模型客户端、凭据存储、会话仓库、租约心跳
    ↓ 已认证本地Agent连接 + Operator业务IPC
Python RuntimeOwner: 全局控制策略、队列、epochs、显示网关、VM生命周期
    ↓
QEMU: 客户机画面和有界HID输入
```

- API Key设置页只显示“已配置/未配置”。“配置/更换”调用main的原生凭据录入适配器；不提供时使用原生文件选择导入密钥文件，由main读取，renderer只收到布尔状态。此适配器是新增工程项，不假设Electron内置密码输入Dialog。不得用普通renderer密码框、剪贴板轮询或localStorage临时承载密钥。落盘使用系统凭据库/可用的安全加密存储，无法安全持久化时提供仅当前进程使用。
- main通过受限认证的Agent客户端接现有observe/act业务面，不把Agent认证信息发给renderer；不开放任意QMP、Shell、HTTP代理或文件路径。backend中的 `agent_ipc.py` 为共享后台代理，不等于已经存在Electron内置Agent。
- 新 `palette-media` 受限协议只解析由main签发的媒体引用，校验webContents归属、MIME和生存期；禁止任意路径遍历或代理远程URL。现有app协议继续仅服务应用包。display.ticket是既有短期显示票据特例，只用于只读画面，不保存到日志/会话或分享导出。
- “联网审批”区分模型访问提供方和客户机网络访问：任务启动前说明将向所选模型发送客户机截图；allowNetwork针对客户机。视觉识别只能发现候选高风险动作，无法保证识别客户机内所有删除/联网行为。真正禁网须由Profile离线/网络隔离策略实施；当前能力不能做逐域拦截时不要声称已实现。剪贴板默认关闭，无支持时禁用，不通过noVNC自动同步。权限页面明确能力范围。
- 同VM驱动声明需要共享RuntimeOwner认可，覆盖其他桌面进程和外部MCP；main本地映射只用于快速拒绝。所有变更仍经过后端原有控制、代次、路径和角色策略，UI审批不能扩大后端能力。

## 7. 设计 Token

优先映射现有Tailwind语义变量，统一替换组件内硬编码色值。下面给出sRGB基线，深色不是对浅色简单取反。状态色只用于状态点/Badge；危险动作文字使用单独danger语义，并带文本，不作为第二强调色。

| Token | 浅色 | 深色 | 用途 |
| --- | --- | --- | --- |
| --background | #F7F6F5 | #191918 | 窗口暖灰底 |
| --sidebar | #F1F0EE | #1E1E1C | 列表与导航 |
| --card / --popover | #FFFFFF | #242422 | 工作区/输入/浮层 |
| --muted | #F1F0EE | #30302D | 用户气泡/选中行 |
| --hover | #ECEAE7 | #353531 | hover底 |
| --border | #E8E6E3 | #3B3B36 | 1px装饰分隔 |
| --foreground | #252522 | #ECECE8 | 正文 |
| --muted-foreground | #686862 | #AEAEA5 | 次要信息 |
| --placeholder | #74746C | #A3A39A | 占位文字 |
| --primary / --ring | #2F6FEB | #82ABFF | 焦点/主操作/人工控制 |
| --primary-foreground | #FFFFFF | #111827 | 主按钮文字 |
| --primary-hover | #245BC7 | #A1BFFF | 强调按钮hover |
| --primary-soft | #EAF0FF | #263650 | 接管背景/焦点辅助 |
| --control-border | #2F6FEB | #82ABFF | 人工控制2px描边，不改变布局 |
| --status-running | #24804A | #79C993 | 运行点/Badge |
| --status-pending | #936300 | #E5BE68 | 启停/暂停点与Badge |
| --status-failed | #B33434 | #F09A9A | 故障点与Badge |
| --status-offline | #73736B | #A5A59A | 关机/断线点 |
| --danger | #B42318 | #FFAAA2 | 危险操作文字/确认按钮 |
| --danger-foreground | #FFFFFF | #2B1010 | 危险按钮文字 |
| --agent-scrim | rgba(247,246,245,.12) | rgba(0,0,0,.16) | Agent只读提示，不遮住内容 |
| --modal-scrim | rgba(20,20,18,.28) | rgba(0,0,0,.52) | Dialog/抽屉遮罩 |

| 类别 | Token/值 | 使用规则 |
| --- | --- | --- |
| 字体 | Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif | 本地Inter，不请求网络；代码用系统monospace |
| 字号 | caption12/18；body13/19.5；chat14/21；title16/24；page20/30 | 正文400；标题/按钮500–600；不以11px承载必要说明 |
| 圆角 | xs6；md12；workspace20；pill999 | 按钮/菜单6，卡片/输入12，工作区20 |
| 间距 | space-1=4、2=8、3=12、4=16、5=20、6=24、8=32、12=48 | 布局均按4px网格；1px为边框特例 |
| 图标/状态 | icon16，空状态32；dot6–8 | 线性图标stroke1.5–1.75；配32px点击区 |
| 焦点 | 2px ring，offset2 | 浅色#2F6FEB/深色#82ABFF；focus-visible时出现 |
| 阴影 | 页面/面板none；浮层浅色0 8px 28px #00000014，深色0 8px 28px #00000052 | 禁止聊天气泡/画面卡片常驻强阴影 |
| 动效 | fast150ms；normal180ms；max200ms；ease-out | 折叠/展开/遮罩；reduced-motion时0ms并去除波纹/旋转位移，保留静态状态图标 |

主题以document根data-theme和系统prefers-color-scheme驱动，跟随系统时不闪白；main窗口底色/标题栏overlay跟随同一主题。高对比度模式用系统色增强焦点与边界，状态文字继续可见。

## 8. 实现阶段、验收标准与测试

各阶段以功能开关渐进落地，不一次性重写所有业务。下表的测试是交付要求，本文编写不等于这些场景已运行。

| 阶段 | 实现范围 | 必须通过的验收 |
| --- | --- | --- |
| P0 契约与边界 | 新模型/方法schema、main协调器状态机、共享VM驱动声明设计、组件受控接口；移除演示路径 | Vitest：方法类型/运行时错误、非法vm_id、schema边界、状态转换、重复requestId；后端测试证明跨客户端互斥。凭据/模型调用不出现在renderer bundle |
| P1 应用壳与路由 | 标题栏、①②、主页/管理/设置、三把手、localStorage、命令面板 | Vitest：布局恢复/非法存储/阈值；Playwright：1440/1280/1279/1100/缩放后1099视口，三个把手拖动/双击/键盘，Ctrl+B/J，设置分类与返回恢复，无应用级横向滚动 |
| P2 会话与聊天 | 会话仓库、VM绑定、分组/最近/搜索、消息/草稿/附件、受控输入与审批外观 | Vitest：分页/排序/归档/草稿隔离；Playwright：新建必须选VM、切会话联动画面、标题编辑、上下文菜单、IME回车、长代码、断线草稿保留；重开应用历史恢复 |
| P3 工作区与人工控制 | 提取Console、溢出工具栏、空状态、main租约、全屏、回放 | Vitest：InputQueue顺序/坐标/滚动/epochs，faulted禁写；Playwright：手动接管、Ctrl+Alt释放不拦Ctrl+Alt+Del、失焦/全屏退出/断画面释放、缩放100%、回放禁输入、抽屉焦点恢复 |
| P4 内置Agent与审批 | main视觉循环、provider配置、事件流、审批策略、控制转交 | Vitest：批准/拒绝/过期/双击、接管与审批/act竞态、stop期间迟到模型结果、释放未确认、同VM两会话竞争、断流补发/去重/乱序/epoch重置；Playwright：新建→执行→审批→接管→交还→完成完整路径 |
| P5 设置与异常恢复 | 密钥native适配/导入、权限、暂停能力协商、根目录切换、退出恢复 | Vitest：不支持能力禁用、权限版本、secret不返回/不落日志、退出失败保持状态；Playwright：保存/取消/离开确认、仅关闭暂停任务但VM继续、完全退出、MCP托管警示、崩溃后interrupted不自动续跑 |
| P6 视觉与实机验收 | 中英开发文档、简中产品文案、深浅主题、可访问性、真实QEMU | 两主题截图比对；Windows/macOS标题栏避让；键盘与读屏抽检、reduced-motion、200%缩放；真实VM上证明互斥、释放、继续观察与生命周期，形成验收记录 |

### 8.1 建议测试文件与关键断言

| 测试文件/层 | 覆盖重点 |
| --- | --- |
| desktop/tests/layout.test.ts | viewport与用户偏好优先级；宽度总和；存储损坏/禁写；折叠不会覆盖正常宽度 |
| desktop/tests/agent-store.test.ts | bootstrap/subscribe间事件不丢；重复seq；delta offset；丢包补发；旧epoch/终态拒绝迟到事件；unsubscribe/StrictMode无重复 |
| desktop/tests/agent-coordinator.test.ts | 每VM原子认领；审批期间零动作；takeover成功前人工零输入；停止丢弃后续动作；恢复前重新observe |
| desktop/tests/approval.test.ts | actionDigest/epochs/策略/期限绑定；重复决定幂等；拒绝不执行；人工接管后旧审批无效 |
| desktop/tests/ipc.test.ts + input.test.ts | 扩展既有IPC与输入测试；拒绝未信任frame、越权媒体ID、非法事件、payload超限；保留旧输入顺序断言 |
| desktop/tests/shutdown.test.ts + vm-settings.test.ts | 保留原退出/硬件回归；新增运行任务停止、释放失败与未保存状态 |
| desktop/e2e/agent-workflow.spec.ts | 用可控provider和受管截图驱动完整主流程，检查系统消息、结果步数与接管次数 |
| desktop/e2e/layout.spec.ts | 实际Electron内容宽度、窄列/抽屉、视图切换、全屏、焦点、主题及溢出菜单 |
| desktop/e2e/exit.spec.ts / windows-ui.spec.ts / vm-settings.spec.ts | 扩展现有用例，保留创建向导、硬件设置、窗口交互和MCP退出行为 |
| tests/unit + tests/operator_ipc + tests/integration | 后端驱动声明、旧epochs动作拒绝、输入释放失败、跨桌面/MCP竞争、暂停恢复状态 |
| tests/e2e + 真实桌面验收 | 显式启用真实QEMU；确认实际鼠标/按键落点、Ctrl+Alt+Del/Super、3秒租约过期及不同宿主平台 |

运行已有检查：`npm --prefix desktop run build`、`npm --prefix desktop test`、`npm --prefix desktop run test:e2e`；触及后端时执行 `uv run pytest`、Ruff、mypy；文档执行 `uv run python scripts/check_docs.py`。纯文档交付不应宣称应用回归已通过。

### 8.2 发布阻断条件

有以下任一情况不得把内置Agent标记为可用：接管未确认就接受人工输入；release未确认仍恢复Agent；同VM两个客户端能同时写入；审批后执行的动作与批准摘要不同；renderer能读取密钥；noVNC能直接发送输入；事件重连重复执行旧动作；回放画面能接收输入；应用崩溃恢复自动继续旧计划。

实机证据必须记录宿主平台、QEMU/后台版本、Profile、客户机镜像、步骤、实际结果与日志。模拟provider、跳过的硬件测试和Playwright截图只能证明UI/契约行为，不能代替真实QEMU控制权验收。
