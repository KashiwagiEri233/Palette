# Palette Desktop GUI and Built-in Agent Design Specification

**English** | [简体中文](desktop-agent-design.zh-CN.md)

Design baseline 1.0, 2026-10-15. Audience: product, design, React/Electron, Python backend, and QA engineers. This is the broader target specification. The built-in pi SDK now implements chat, model credentials, VM visual tools, context compaction and conversation branching; see the [Desktop guide](desktop.md) for the delivered boundary. Approval, replay and the full interface below remain proposals. All product copy remains Simplified Chinese. The shared, type-checkable API draft is [desktop-agent-contract.ts](desktop-agent-contract.ts).

## 0. Architecture and verified implementation boundaries

A VM is the work target, a conversation records a task, and the workspace provides live observation and human takeover. Every conversation permanently belongs to one VM. Selecting it changes the displayed VM without stopping a background task. Home, VM management, and Settings are the three primary destinations. A conversation is a working route under Home, so Home remains selected in the global rail.

Keep Electron/electron-vite, React 19, TypeScript, Tailwind v4, local Inter, local icons, and lucide-react. Keep noVNC `viewOnly=true` even during human control; send input through the separate existing control channel. Main owns credentials, provider requests, the Agent loop, leases, and heartbeats. The Python shared RuntimeOwner remains authoritative for control, action queues, and epochs.

| Verified source behavior | Required change |
| --- | --- |
| App.tsx combines management, creation, details, and exit flows | Split the shell and routes below while retaining business validation |
| Console.tsx implements noVNC, InputQueue, coordinate mapping, fullscreen, and release on blur | Extract DisplaySurface and the input adapter; move leases and heartbeat to main |
| shared/types.ts request returns unknown; main has a method allowlist | Add per-method types and runtime validation without exposing arbitrary IPC |
| agent-chat, ai-agent-input, approval-card contain demonstration logic | Add controlled modes/slots to those components; remove timers, example content, and automatic submissions from production paths |
| ai-agent-input exports PromptInput with a mock enhancer and hardcoded models | Reuse its editor/attachment layout; use real model capabilities and onSend/onStop |
| approval-card currently implements a questionnaire with automatic radio submission | Add explicit decision mode; permission requires clicking “允许” |
| Desktop IPC has no pause/resume or Agent subscription | Negotiate new capabilities; disable unsupported pause with explanatory copy |
| Release currently uses Ctrl+Alt+G; minimum window is 960×640 | Add Ctrl+Alt with compatibility behavior; change minimum to 1100×700 |

ThinkingSteps displays public progress summaries and observed operations, never private chain-of-thought or system prompts. Guest screenshots, text, and tool results are untrusted content and cannot grant authority.

## 1. ASCII wireframes

These diagrams show relationships rather than exact scale. ① global rail, ② VM/conversation navigation, ③ conversation, ④ workspace.

### 1.1 Home

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ [‹][›][侧栏]   文件  编辑  视图  帮助                        [—][□][×]      │
├────┬──────────────────┬────────────────────────────────────────────────────┤
│ ①  │ ② Palette ▾ [⌕] │                    主页                            │
│ 主 │ [+ 新对话]       │                                                    │
│ 页 │ ● 开发环境 ▾     │              想让 Agent 做什么？                   │
│    │   整理下载目录   │       ┌────────────────────────────────────┐       │
│ 虚 │   安装编辑器     │       │ 描述你的任务…                       │       │
│ 拟 │ ○ 测试环境 ▾     │       │ [+]                 模型 ▾     [↑] │       │
│ 机 │   暂无对话       │       └────────────────────────────────────┘       │
│    │                  │          [● 开发环境 ✓] [○ 测试环境]               │
│ 设 │ 最近             │       运行中的虚拟机                               │
│ 置 │ 整理下载目录     │       [缩略画面 | 开发环境 · CPU/内存      打开]    │
│    │   开发环境       │       最近会话                                      │
│ ●  │ 后台已连接       │       [导入磁盘镜像] [从 ISO 安装]                 │
└────┴──────────────────┴────────────────────────────────────────────────────┘
```

Home uses a 960px maximum content column and 720px maximum composer. Top space varies from 32 to 96px with height. Target chips are single selection. Without a VM, disable send and offer creation. Missing utilization is “—”, never configured memory presented as usage. First send creates the conversation and submits idempotently, then navigates to its route. Failure retains text, attachments, and target.

### 1.2 Conversation and workspace

```text
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ [‹][›][侧栏]  文件 编辑 视图 帮助                                      [—][□][×]    │
├────┬───────────────────┬─────────────────────────┬───────────────────────────────────┤
│ ①  │ ② Palette ▾ [⌕]  │ ③ 整理下载目录          │ ④ ┌─────────────────────────────┐ │
│ 主 │ [+ 新对话]        │ [● 开发环境]  [⋯][工作区]│   │ 开发环境 运行中 x86_64 [收起]│ │
│ 页 │ ● 开发环境 ▾ [+]⋯│─────────────────────────│   │ [电源组] │ [接管] [⋯]       │ │
│    │   整理下载目录 ◌  │      ┌────────────────┐ │   ├─────────────────────────────┤ │
│ 虚 │   安装编辑器      │      │ 整理下载文件夹 │ │   │   ┌─────────────────────┐   │ │
│ 拟 │   展开显示        │      └────────────────┘ │   │ ‹ │     虚拟机画面      │ › │ │
│ 机 │ ○ 测试环境 ▾      │ 用时 2 分 5 秒 ▾        │   │   │  [✦ 接管任务]      │   │ │
│    │   暂无对话        │ 执行了 6 个操作 ▾       │   │   └─────────────────────┘   │ │
│ 设 │ 最近              │ ┌─────────────────────┐ │   ├─────────────────────────────┤ │
│ 置 │ 整理下载目录      │ │ 允许删除这些文件吗？│ │   │1920×1080 · Agent 控制中      │ │
│    │   开发环境        │ │ 风险说明 / 查看计划 │ │   └─────────────────────────────┘ │
│    │                  │ │ [不允许]    [允许]  │ │                                   │
│    │                  │ └─────────────────────┘ │                                   │
│    │                  │ ┌─────────────────────┐ │                                   │
│ ●  │ 后台已连接        │ │让 Agent 操作这台…   │ │                                   │
│    │                  │ │[+] [开发环境] 模型 ■│ │                                   │
│    │                  │ └─────────────────────┘ │                                   │
└────┴───────────────────┴─────────────────────────┴───────────────────────────────────┘
```

The workspace toolbar is a single 44px row; the diagram separates groups for clarity. Use overflow instead of wrapping. Hiding ④ expands ③, with message content capped at 760px.

### 1.3 VM management and details

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

Retain the three-step creation wizard: source → configuration → confirmation, including advanced hardware options. Dialog width is 640px, up to 800px for advanced settings; maximum height is 85% of available height, with scrolling content and fixed footer. Reuse CreateVm, HardwareFields, ResourceInputs, VmSettings, validation, capability detection, and discard confirmation. Configuration is a summary with “编辑配置”; honor backend editability for fields requiring shutdown.

### 1.4 Settings

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
│ 设 │ 关于             │                                  [取消] [保存]     │
│ 置 │                  │                                                    │
│ ●  │ 后台已连接       │                                                    │
└────┴──────────────────┴────────────────────────────────────────────────────┘
```

Settings replaces ② with categories rather than adding a fifth column. Restore navigation width, expansion, and scroll on return. Forms have maximum width 720px, labels 144px, and field spacing 24px. Save explicitly; appearance previews and saves immediately, credentials submit independently. Preserve unsaved category edits and confirm before leaving with “继续编辑 / 放弃更改 / 保存”.

| Category | Fields and behavior |
| --- | --- |
| Runtime | Configuration directory/choose, read-only runtime directory, backend version/reconnect, environment checks with item/result/details and run-check action |
| Agent | Main-supplied provider/model lists, credential status, default effort, maximum steps; default100, integer1–1000 subject to main limit; reject unknown model |
| Permissions | Approval for deletion/submission/shutdown/network/clipboard; guest networking and clipboard off by default; denied permission overrides approval exemptions; unsupported controls disabled with explanation |
| Appearance | Light/dark/system; preview and persist immediately |
| Shortcuts | Platform-specific shortcut list and reserved keys; read-only in the initial version |
| About | App/backend versions, license, docs; main opens links; no endpoint tokens |

Provider/model changes during execution affect the next run only; show the current run's actual configuration. Missing credentials preserves draft editing and links to settings. Before first execution, disclose that task screenshots go to the selected model service and obtain explicit session Agent authorization as part of starting; this does not approve later risky actions.

### 1.5 Narrow windows and collapsed panels

```text
1100–1279 CSS px: VM navigation becomes a 48px icon strip
┌────────────────────────────────────────────────────────────────────┐
│ 标题栏                                                   [—][□][×] │
├────┬────┬───────────────────────┬──────────────────────────────────┤
│①48 │②48 │③ 标题 [● 开发环境]  │④ 开发环境 运行中 [接管] [⋯]    │
│主页│[+] │  消息流               │  ┌──────────────────────────┐   │
│管理│[A] │                       │  │        实时画面          │   │
│设置│[B] │                       │  │      [✦ 接管任务]       │   │
│ ●  │[⌕] │  [输入框]             │  └──────────────────────────┘   │
└────┴────┴───────────────────────┴──────────────────────────────────┘

<1100 CSS px: workspace becomes a modal right drawer
┌────────────────────────────────────────────────────────────────────┐
│ 标题栏                                                    [×]     │
├────┬────┬──────────────────────────────────────────────────────────┤
│①   │②   │③ 对话主区       ║④ 抽屉 [开发环境] [关闭]               │
│    │    │                  ║ [接管] [⋯]                            │
│    │    │ 消息流/遮罩      ║ ┌─────────────────────────────────┐  │
│    │    │                  ║ │             画面                │  │
│    │    │ [显示工作区]     ║ └─────────────────────────────────┘  │
└────┴────┴──────────────────╨──────────────────────────────────────┘

Hide ②: ① | ③ | ④. Ctrl+B restores ②.
Hide ④: ① | ② | ③ expands. Ctrl+J restores ④.
```

The native minimum is 1100×700; `<1100` remains a fallback for effective CSS viewport changes, small-screen restoration, zoom, and tests. Drawer width is `min(720px, 100vw - 24px)`; below 544px it fills the viewport and waives the 520px workspace minimum. Switching to drawer mode does not open it automatically. Screenshot selection opens the drawer at that step.

## 2. Dimensions, spacing, layers, and responsive rules

| Region | Default/range in CSS px | Spacing and behavior |
| --- | --- | --- |
| Window | Initial1440×900, minimum1100×700 | 100dvh; no page-level scrolling; min-width/min-height:0 on flexible children |
| Title bar | Height48 | z40; 32px buttons; non-draggable interactive regions; reserve actual Windows overlay dimensions or 80px for macOS traffic lights |
| ① Rail | Default48, adjustable48–64 | 32px buttons, 8px inset, 16px icons, bottom status; fixed48 in narrow layouts |
| ② Navigation | Default260, min240, max280, hidden0 | 8px inset, 16px groups, header48/footer40, rows32; <1280 becomes48 with 240px conversation popover |
| ③ Conversation | Default420, preferred380–460, min360, max640 | Header56; horizontal20; turn gap24; <1100 min320; expands when workspace hidden, content max760 |
| ④ Workspace | Remaining width, min520 | Deduct24 total outer gutter; radius20; padding12–16; toolbar44; status minimum28; drawer may waive minimum |
| Three separators | Visible1, hit target8 | z10; hover/focus blue; no extra occupied width; hidden-panel handles disappear |
| Message timeline | Independent ScrollArea, flex1 | User bubbles max85%, padding12; assistant unboxed; code scrolls horizontally; URLs wrap |
| Composer | Card min120; text max180; whole area max240 | Outside16, inside12, radius12, controls32; preserve at least240px timeline at height700 |
| Tools and approval | Tool rows28; thumbnails96×54 | Approval padding16, buttons32, risk gap8; group consecutive ≥3 operations, interrupted by approvals/errors |
| Display | Contain at actual aspect ratio | Inner radius12; inset16, minimum8; 32px replay controls; 100% mode uses independent two-axis scrolling |
| VM management | Margin24; header40; rows48 | Search280; narrow mode moves architecture/count to details; retain actions |
| Settings | Categories reuse②; form max720 | Margin24–32, section32, field24; main area<640 stacks labels and uses category dropdown |

Width budget at1440: `48 + 260 + 420 + 3×1 + 24 + 685 = 1440`. At1280, workspace is525. If the rail is64, reduce chat to404 before shrinking navigation toward240. If minimums still cannot fit, force navigation into48px mode; never overlap or introduce application-level horizontal scrolling.

The rail has a small adjustable range to reconcile its approximately48px width with three draggable separators. Each separator adjusts its left panel: double-click restores rail48, navigation260, or chat420 respectively. Workspace takes the remainder. Wrap the installed v3 resizable primitives and convert pixel constraints into percentages using ResizeObserver; do not pass520 as a percentage. Deduct fixed gutters before conversion. Persist after dragging ends.

Layout priority: fullscreen → drawer → explicit hidden state → automatic breakpoint mode → preferred widths. Explicitly hiding② overrides automatic48px mode. Below1280 Ctrl+B toggles hidden/48px; at larger widths restore the previous full width. Never persist automatic collapse as user preference.

Store only version, railPx, sidebarPx, chatPx, sidebarHidden, workspaceHidden in `localStorage['palette.layout.v1']`; keep category width separately. Validate and clamp on restore; fall back safely if storage fails. Do not store messages, credentials, leases, or approvals there. Resizing must not unmount noVNC or restart Agent; a stable DisplaySessionController owns the connection across inline/drawer presentation changes. Switching VM rebinds it.

Separators support arrows8px, Shift+arrows32px, Home/End, and Enter reset; label them and expose current/range values. Disable animation while dragging.

| Layer | Content | Rule |
| --- | --- | --- |
| 0–10 | Base/panels/separators | Independent scrolling; pinned input/toolbars |
| 20 | Display scrim, cursor, replay, takeover | Confined to display; takeover centered within middle35%–65% vertical area |
| 30 | Sticky headers/composer/notices | Never cover native window controls |
| 40 | App title bar | Respect native control hit regions |
| 50 | Menus/tooltips | Existing portals; return focus on close |
| 60–70 | Drawer/scrim, confirmation Dialog | Confirmation above drawer; focus trapping |
| 80 | Sonner | Avoid approval buttons; persistent failures also remain inline |

## 3. React tree and reuse mapping

New names below are business compositions, not replacements for existing primitives. Use `renderer/components/{shell,navigation,chat,workspace,settings}` and `renderer/lib` for hooks/state. Adapt existing components where specified; remove all production demo content. Local icons first, then lucide-react; no network-loaded fonts/icons.

```text
PaletteApp [new; split App.tsx]
├─ AppProviders [new: theme, normalized event store, focus, shortcuts]
├─ WindowTitleBar [new: Button, DropdownMenu, Tooltip, Kbd]
├─ DesktopShell [new: ResizablePanelGroup, ResizablePanel, ResizableHandle]
│  ├─ GlobalNavRail [new: Sidebar, adapted SidebarNav, Button, Tooltip]
│  │  └─ BackendConnectionIndicator [new: StatusIndicator, Tooltip]
│  ├─ NavigationPane [new: Sidebar, ScrollArea]
│  │  ├─ WorkspaceSwitcher [new: DropdownMenu, Button]
│  │  ├─ NewConversationButton [new: Button, DropdownMenu]
│  │  ├─ VmConversationGroup [new: SidebarGroup, ContextMenu, Badge]
│  │  │  └─ ConversationRow [new: SidebarItem, Tooltip, StatusIndicator]
│  │  ├─ RecentConversations [new: ConversationRow]
│  │  └─ CollapsedVmSwitcher [new: DropdownMenu, Button, Tooltip]
│  └─ RouteOutlet [new]
│     ├─ HomePage [new: EmptyState, ScrollArea]
│     │  ├─ HomeComposer [AgentComposer reuse]
│     │  ├─ TargetVmChips [new: ButtonGroup, Badge]
│     │  ├─ RunningVmCard [new: Button, Badge, StatusIndicator]
│     │  └─ QuickCreateActions [new: Button, existing wizard]
│     ├─ ConversationPage [new: ResizablePanel, ResizableHandle]
│     │  ├─ AgentChatPanel [adapt agent-chat to controlled layout]
│     │  │  ├─ ConversationHeader [new: Badge, DropdownMenu, Button, Tooltip]
│     │  │  ├─ MessageTimeline [new: ScrollArea, EmptyState]
│     │  │  │  └─ MessageItem [new: role renderer, safe Markdown/code]
│     │  │  │     ├─ RunProgress [new: ThinkingSteps, ThinkingIndicator]
│     │  │  │     ├─ ToolStepGroup [new: ToolChips, Button]
│     │  │  │     │  └─ ToolStepRow [new: Button, Tooltip, thumbnail]
│     │  │  │     ├─ AgentApprovalCard [adapt ApprovalCard decision mode, Button]
│     │  │  │     ├─ MessageActions [new: Button, Tooltip]
│     │  │  │     └─ RunResultCard [new: Badge, Button]
│     │  │  └─ AgentComposer [adapt PromptInput from ai-agent-input]
│     │  │     ├─ VmAvailabilityBanner [new: Button, StatusIndicator]
│     │  │     └─ ModelControls [new: DropdownMenu, Button, Tooltip]
│     │  └─ VmWorkspace [new; same surface in Dialog drawer mode]
│     │     ├─ VmToolbar [new: ButtonGroup, DropdownMenu, Badge, Tooltip]
│     │     ├─ ReplayBanner [new: Button]
│     │     ├─ DisplaySurface [extract/reuse Console.tsx; read-only noVNC]
│     │     │  ├─ DisplayStateView [new: EmptyState, Button, skeleton CSS]
│     │     │  ├─ AgentControlOverlay [new: Button, StatusIndicator]
│     │     │  ├─ HumanControlLabel [new: Badge, Kbd]
│     │     │  └─ ReplayControls [new: Button, Tooltip]
│     │     └─ WorkspaceStatusBar [new: StatusIndicator, Tooltip]
│     ├─ VmManagementPage [new: Input, Table, Button, EmptyState]
│     ├─ VmDetailPage [new: Tabs, VmWorkspace, existing LiveStatus/Configuration]
│     └─ SettingsPage [new: Sidebar, ScrollArea, Label, Input]
│        ├─ RuntimeSettings [reuse environment page, Table, Button]
│        ├─ AgentSettingsForm [new: NativeSelect, Input, Button]
│        ├─ PermissionSettingsForm [new: Checkbox, Button, Dialog]
│        ├─ AppearanceSettings [new: ButtonGroup]
│        ├─ ShortcutSettings [new: Kbd, Table]
│        └─ AboutSettings [new: Button]
├─ CreateVmWizard [reuse CreateVm, Dialog, HardwareFields, ResourceInputs]
├─ VmSettingsDialog [reuse VmSettings]
├─ CommandPalette [new: Command, Dialog]
├─ ConversationSearch [new: Command, Dialog]
├─ ConfirmActionDialog [new: Dialog, Button]
├─ ExitDialog [reuse existing dialog, add active task summary]
└─ ToastHost [reuse Sonner]
```

SidebarNav must also replace its sample navigation/state with controlled real data. Safe Markdown rendering must reject raw HTML, script URLs, and automatic remote images; main validates external link opening. Select an implementation during engineering, retaining these requirements.

## 4. State inventory and availability

Model connection, VM lifecycle, run, control, display connection, and replay independently. Waiting for approval still permits observation; replay does not imply Agent is paused. Every business component above belongs to a row below; leaves inherit their family's states and pure containers inherit their route. A dash means not applicable.

| Component family | Empty | Loading/error | Disabled | Agent running | Human takeover | Approval | Replay |
| --- | --- | --- | --- | --- | --- | --- | --- |
| App/providers/shell/routes | Home | Connection skeleton/persistent reconnect | Exit locks mutations | Background unaffected | Current control projection | Pending count | Independent selection |
| Titlebar/rail/connection | Home default | Connecting label/disconnected details | Back/forward without history | Navigation enabled | Release before leaving console | Visible pending state | History does not cancel task |
| Navigation/switchers | Create VM | Group skeleton/inline retry | Disconnected writes | Spinner | Mouse icon | Blue dot and text | Selected conversation retained |
| NewConversationButton | Choose/create VM | Creating/error retains text | In-flight duplicate | May create another conversation | No transfer | Does not replace approval | New conversation opens live |
| Groups/rows/recent | 暂无对话 | 32px skeleton/group retry | Archived read-only; stopped selectable | Spinner | Mouse | Dot plus text | Step remains tied to owner |
| Home/target/quick-create | Creation actions | Card skeleton/error banner | No target prevents send | Busy target links to owner | Current ownership shown | Recent badge | — |
| RunningVmCard | No running VM text | Thumbnail skeleton/placeholder | Prevent repeated open | Status badge | Text | Pending badge | Open live |
| Chat/header | 新对话 | History/header skeleton/retry | Delete/title submission | Running label | 人工控制中 | 等待审批 | Workspace toggle retained |
| Timeline/message | Task suggestions, no demos | History/streaming/local error | Archived edit | Stream deltas | System message | Approval card | Highlight step |
| RunProgress | Hidden without summary | Observing/interrupted | — | Collapsible elapsed/steps | Active time paused | Active time paused | Expand only |
| Tool groups/rows | Hidden | Active spinner/failed or unknown | Missing screenshot | Group ≥3 | Paused | Risk step blocked | Selected step highlighted |
| Approval | Hidden | Submitting/error retained | Decided/expired/mismatch | Scheduler waits | Old request expires | Risks/plan/two buttons | Valid only for live context |
| MessageActions/result | Hidden without result | Feedback save/retry | Retry during active run | Copy generated text | Count takeover | No completion card | Open final capture |
| Composers/model controls | Placeholder | Submit/error preserves draft | Stopped/busy/no key/archived | Stop button, editable draft | Return entry | Stop enabled/send blocked | Return live before send |
| Availability banner | Select VM | Starting/start error | Lifecycle busy | Link to owner | Release fault | Locate approval | History notice |
| Workspace/toolbar | Select VM | Lifecycle busy/logged errors | Lifecycle matrix | Takeover | Return | Takeover allowed | Live before dangerous actions |
| Replay banner/controls | Hidden without capture | Skeleton/retry/removed image | First/last boundary | Warn task still running | Release before replay | Approval remains readable | Position and live action |
| Display/state view | Stopped/start | Connecting/fault code/logs | View-only/input blocked | Light scrim/ripple | Blue border/release label | Read-only | Static image/input blocked |
| Control overlay/label | — | Taking over/failure | Transfer pending | Blue pill | Human label | Takeover expires request | No live cursor ripple |
| Workspace status | Resolution “—” | Connecting/persistent control fault | — | Agent control | Human control | View-only/waiting | History/view-only |
| Management/detail/status/config | Create/no matches | Skeleton/local retry | VM mutation pending | Owner link | Ownership visible | Approval link | Shared console state |
| Settings and six forms | Unconfigured | Read/save/field errors | Unsupported/saving | Defer disruptive changes | Release before root/network change | Policy expires approvals | — |
| Creation/hardware dialogs | Initial fields | Probe/save/errors retain input | Unsupported/not editable | Stop task before hardware edit | Release first | No implicit authorization | — |
| Command/search | No results | Index load/failure | State-dependent actions | Stop/locate | Release | Jump to request | Return live |
| Confirm/exit | — | Busy/retained error | Duplicate submit | List active tasks | Release before exit | Revoke on full exit | History persists |
| ToastHost | Hidden | Readable result | — | Meaningful changes only | Release reason | Never replaces card | Avoid display center |

### 4.1 Lifecycle actions and toolbar

| VM state | Start | Shutdown | Restart | Pause/resume | Dangerous actions | Display/input |
| --- | --- | --- | --- | --- | --- | --- |
| STOPPED | Yes | No | No | No | Reset/delete | Empty/start; no input/run |
| STARTING | No | No | No | No | Force stop | Skeleton; no invented progress percentage |
| RUNNING | No | Yes | Persistent VM | Pause if supported | Force stop; no reset/delete | Connected and valid control required |
| STOPPING | No | No | No | No | Force stop unless conflicting request pending | Last frame/shutting down; input blocked |
| PAUSED | No | Backend-supported | Backend-supported/persistent | Resume if supported | Force stop; no reset/delete | Frozen frame and paused label; Agent paused |
| FAILED | Only after no active instance confirmed | No | Only if backend supports | No | Force if process remains; reset/delete only when absent | Error/logs/restart, otherwise start |

All actions also honor actual backend capabilities and persistent/transient VM rules. Force stop/reset/delete are danger text with icons and secondary confirmation naming the VM and effects. Reset discards disk modifications; delete removes the definition and managed disk, not the source image, and explicitly includes all conversations/captures. Never leave orphan vm_id records. Active Agent must stop and release before lifecycle mutation. A direct user toolbar action does not require another Agent approval card.

Toolbar container≥960 shows all groups;720–959 moves shortcuts, zoom, reconnect, and settings to overflow;520–719 retains identity, primary power action, takeover/return, fullscreen, and overflow. Name max160px or compact96px; architecture moves into identity tooltip first. Never wrap the toolbar. Ctrl+Alt+Del and Super require human control and live view; do not use RFB.sendKey.

### 4.2 Navigation, chat, and replay details

- VM groups sort by name unless VM pinning is later introduced. Conversations sort pinned first, then updatedAt descending. Show five initially with reversible count-labelled expansion. Reduce stopped-VM decoration contrast rather than applying opacity to the entire text container.
- Recent includes every unarchived conversation even if also visible in its group. Both reference one ID and selection. Truncate titles with full tooltip; VM subtitle12px. Hover actions also appear on focus-within.
- Conversation context menu: rename, pin/unpin, archive, delete. VM menu: allowed start/shutdown, settings, management. Archive an active conversation only after stop confirmation. Clear/delete stop first and require confirmation; clear preserves conversation/VM identity, delete removes records/media. Search excludes archive unless “包含已归档” is enabled.
- Title edit: Enter saves, Esc cancels, reject empty, and do not overwrite an unsaved title with a remote update. Search groups VM/conversation results and displays ownership.
- Autoscroll only within80px of the bottom; otherwise offer “有新消息 ↓”. Preserve per-conversation draft, scroll, expanded tools, and replay selection. Selecting a conversation never acquires control; leaving a controlled VM/console releases first.
- Execution elapsed excludes approval/human waits; details also show wall-clock elapsed. Sensitive input is “输入了受保护文本”. Feedback remains local unless separately authorized.
- Retry creates a new run and observes the current screen; it does not replay input. Preserve old replies and failures; reconcile unknown outcomes first. Completion card includes actual step/takeover counts; cancellation/failure is never labelled successful.
- Selecting a step prefers its after screenshot, otherwise before; show timestamp, resolution, and before/after. Replay navigation only counts steps with images and also exposes original operation index, e.g. “回放5/12 · 操作7”. Missing captures are not selectable.
- Replay does not pause a background run; say it is still executing. Input is blocked. Takeover returns live and obtains a fresh frame before acquiring. Entering replay while controlling releases first. Coordinate mapping accounts for contain padding or100% scroll offsets; reject old displayEpoch inputs.

## 5. Interaction sequences

```text
User       Renderer/Preload       Main AgentCoordinator          Shared daemon
 | send          |                         |                           |
 |-------------->| create(vm_id)---------->| persist conversation      |
 |               | start(requestId)------->| atomically claim VM        |
 |               |                         | check control/observe---->|
 |               |<---run/step/message-----|<----frame + epochs---------|
 |               |                         | plan, check policy         |
 |<--approval----|<----pending approval-----| pause scheduling           |
 | allow-------->| decide----------------->| validate digest/context    |
 |               |                         | bounded action----------->|
 |               |<----step/capture--------|<----receipt----------------|
 | takeover----->| takeover---------------->| block/cancel future steps |
 |               |                         | control.acquire---------->|
 |               |                         |<---release + new epoch-----|
 |               |<----human state---------| own lease/heartbeat        |
 |<--system msg--|                         |                           |
 | input-------->| typed input------------>| validate/forward---------->|
 | return------->| return------------------>| release inputs/lease------>|
 |               |                         |<---release_confirmed-------|
 |               |                         | fresh observe------------>|
 |               |<----observing-----------|<---current frame-----------|
 |               |                         | replan/reapprove if needed |
 |               |<----completed/result----|                           |
```

1. **Create**: require vm_id. A stopped VM can have a conversation and draft, but cannot run. Start from the banner, wait until running/display ready, then require send again; do not silently execute an old draft. Requests are idempotent.
2. **Execute**: main persists the user message and atomically claims the VM, while backend verifies control/action eligibility. Loop: observe → plan → approval check → bounded action → receipt → observe. One driver per VM; different VMs may run within a global concurrency limit.
3. **Approve**: deletion, submission, shutdown, network, and clipboard candidates block according to policy. Card states target, effects, public plan, and single-action scope. Deny cancels the action and pauses with approval_denied; offer task adjustment/stop, never an automatic same-risk retry. Allow binds a digest and context; backend restrictions still apply.
4. **Take over**: under the per-VM coordinator lock enter taking_over, block new steps, cancel queued work/provider calls, and acquire through backend to release held inputs and advance controllerEpoch. Only an already irreversible bounded transaction may finish. Show “正在接管…”; accept human input only after authoritative lease/release confirmation. Failure remains view-only. Success adds “你已接管控制”, increments takeoverCount, and expires pending approval.
5. **Return**: block/drain InputQueue, release all held inputs and the lease; require release_confirmed before resuming. Fresh observation invalidates old coordinates, plans, frames, and approvals. Resume only a recoverable run with explicit user continuation. Without one, label the action “结束控制”.
6. **Complete**: main terminal event finalizes counts and result, clears ownership, and persists. Late deltas cannot resurrect a terminal run; the next message starts another run.

| Exception | Main/backend response | UI/recovery |
| --- | --- | --- |
| Blur/hide/fullscreen exit/heartbeat failure | Block input, stop queue, release; main listens independently of renderer | Toast reason; Agent stays paused until explicit continue |
| Release unconfirmed/disconnect | faulted, no input or Agent; verify backend after expiry | Persistent “控制释放尚未确认，已暂停输入” and reconnect |
| Display loss/resize/restart | Stop writing, release human, invalidate frames/approvals | Reconnect/fresh observation; never blind-click last frame |
| Another conversation on VM | Atomically reject VM_BUSY with owner ID | “另一会话正在操作此虚拟机 [查看]” |
| External MCP competitor | Shared RuntimeOwner claim/control/queues/epochs, not just local main lock | Must test cross-client exclusion before release |
| Stop/Esc | Cancel provider/future work, release inputs; reconcile unknown outcome | Stopping→stopped; do not power off VM |
| Close window only | Lock new runs, pause/persist Agent, release human; retain daemon/VMs | Existing close choices; warn N active tasks will pause |
| Fully quit | Stop runs, cancel approvals, confirm release, existing RuntimeShutdown | Failure retains dialog/error; retain MCP-hosted connection warning |
| Crash/main restart | Restore history; nonterminal runs become interrupted; query backend | Explicit continue from current frame; never auto-replay |
| Delete/clear conversation or VM | Stop→release→transactional record/media cleanup; VM disk via existing backend | Keep object on failure; deleted VM navigates Home, never rebinds |

### Keyboard and accessibility

| Shortcut | Behavior |
| --- | --- |
| Ctrl+N / macOS Cmd+N | New conversation on current VM or choose VM |
| Ctrl+K / Cmd+K | Command palette with state-aware VM/conversation actions |
| Ctrl+B / Cmd+B | Toggle navigation/categories |
| Ctrl+J / Cmd+J | Toggle workspace/drawer |
| Enter / Shift+Enter | Send/newline; never send during IME composition |
| Ctrl+Alt | Release only when the modifier-only chord completes without another key |
| Ctrl+Alt+G | Retain legacy release; do not forward to guest |
| Esc | Close topmost menu/dialog/drawer, otherwise exit display fullscreen, otherwise stop current Agent; one action per press |

Reserve host shortcuts even with display focus. Do not misinterpret Ctrl+Alt+Del as release; send it explicitly from toolbar. Toolbar/composer focus exits display capture. Refocusing never reacquires. Fullscreen retains fixed exit/takeover controls.

Every icon button needs Chinese aria-label/tooltip,32px target, and visible focus. Use aria-current/expanded; pair status colors with text/icons. Display is a focusable region labelled with VM/control/release instruction; avoid application role. Message log announces controlled progress/approval/completion summaries, not every token/mouse event. Do not steal focus when approval arrives; offer a jump action and focus its heading/risks when opened, never default “允许”.

Trap modal focus and restore trigger, or the next valid row if removed. Keep drag regions off controls. Normal text contrast≥4.5:1 and essential nontext/focus≥3:1; pale decorative borders are not the only cue. The app workflow is keyboard-operable; do not claim guest pixels are screen-reader accessible.

## 6. Renderer models, persistence, and IPC

The complete types and method mapping are in [desktop-agent-contract.ts](desktop-agent-contract.ts), a design draft outside the application build.

| Entity | Key contract | Owner |
| --- | --- | --- |
| Conversation | Immutable required vm_id; title/pin/archive/revision/activeRun/draft | Main repository; renderer cache |
| Message | Conversation/VM/run IDs, role/status/content, attachment refs, public progress, step/approval IDs, result | Main-generated assistant/system roles and relationships |
| ToolStep | Run/index/kind/summary/status, before/after captures, point, actionSeq/error | Real receipts, explicit null captures, screenshot pixel coordinates |
| ApprovalRequest | Run/step/VM, digest, epochs, policy revision, expiry, single_action status | Main plus backend policy revalidation |
| AgentRun | State/model/effort/maxSteps/counts/revision | Main scheduler; terminal states stable |
| ControlView | Owner, epochs, revision, releaseConfirmed | Backend facts projected by main; no bearer lease ID in renderer |
| VmAgentView | Adapted VmRecord plus display/control/run/nullable metrics/features | Backend facts; control_owner=agent does not imply executing run |
| Screenshot/attachment refs | Opaque ID, dimensions/MIME/time/epochs, no arbitrary host path | Main-managed media and retention |
| AgentSettings | Provider/model capabilities/effort/maxSteps/configured bool/policy revision | Main; unsupported effort controls hidden |

Persist conversations under app userData, indexed by configuration-root identity. Use transactions or atomic replacement and recover after crashes. Captures default to30-day retention; text persists until deleted; expired images show “截图已清理”. Main export uses a native save dialog and excludes protected input/host paths by default. Root changes stop tasks and release control first; never map an old vm_id into another root's VM.

Normalize renderer state by IDs for conversations/messages/runs/steps/approvals/vms. Keep selection, replay, scroll anchors, and uncommitted edits separate. Reducers never invoke models/input; mutations require explicit request calls.

### 6.1 Typed methods

Preserve `window.palette.request(method, vmId, payload)` with per-method payload/results. New main-owned methods are not blindly forwarded to Python. Extend the backend allowlist only for actual backend operations.

| Methods | Checks/behavior |
| --- | --- |
| agent.bootstrap | Atomic snapshot and stream cursor, one selected history page |
| conversations.create/list/history/update | VM ownership, revisions, pagination, title/pin/archive/draft |
| conversations.clear/delete/export | Stop/cleanup, native export destination |
| messages.feedback | Local persistence and message ownership |
| agent.start/stop/resume | Idempotency, expected instance, VM exclusion, model/policy/state; observe before resume |
| approvals.decide | Pending run, matching digest/revision/epochs/policy/expiry; repeated decision returns original |
| control.takeover/return/release/input | Main-owned lease; renderer supplies intent, control revision/seq/geometry only |
| screenshots.get / attachments.choose | Scoped IDs, ownership, MIME/size limits, native file choice; backend capture |
| agent.settings.get/update / permissions.update | No secrets; restriction applies immediately/expires approval; relaxation applies to new runs only |
| credentials.configure | Provider and mode only, returns configured boolean |
| window.command | Fixed action enum/trusted window; close still confirms |
| vms.pause/resume | New negotiated backend lifecycle extension with release/epoch invalidation |

Return Result<T> with code, Chinese message, retryable, optional ownerConversationId. Never return raw stacks/tokens/provider headers. Mutations carry requestId and durable deduplication results for recovery. Unknown outcomes require status reconciliation rather than automatic action resend.

The old backend acquire/heartbeat/release remain inside main's adapter. Renderer gets no lease_id after migration. Same-named control.input/release convert the new UI payload into the old backend protocol rather than forwarding raw. Main heartbeats every second; current backend expiry is3 seconds. Unconfirmed release stays blocked.

### 6.2 Subscription and recovery

Add only typed `subscribeAgent(options, listener)` through preload, never ipcRenderer/channel names. Options include client subscriptionId, streamEpoch, afterSeq. Return ready Promise and unsubscribe. Install the fixed-channel listener before invoking registration. Main atomically installs replay plus following live delivery, eliminating the bootstrap/subscription race. Validate recipient webContents and isolate configuration roots.

Events: conversation.upsert/deleted, message.upsert/delta, step.upsert, approval.upsert, run.upsert, control.changed, vm.upsert, settings.changed, stream.reset_required. Envelope: subscriptionId, streamEpoch, monotonically increasing seq, emittedAt; entities carry revision. The shared TS mountAgentStore example illustrates bootstrap/subscription. A real React effect must also clean up a subscription if asynchronous initialization finishes after unmount.

1. One root-level full stream per root identity; selecting a conversation does not add listeners. Do not silently filter events and create sequence gaps. Ignore stale subscriptions/epochs.
2. Deduplicate seq≤lastSeq. A gap pauses incremental merging and requests replay. Text deltas require matching message/revision/offset; mismatch reloads the complete message. Bound the out-of-order queue.
3. Retain replay for10 minutes or10,000 events, truncating at either limit. Expired cursor/changed epoch/overflow triggers reset_required, bootstrap, and resubscribe without re-executing actions.
4. Cap each IPC message at64KiB, merge text deltas every30–50ms, and preserve approval/control/terminal boundaries. Images travel as refs, never event base64. Page/chunk oversized history.
5. Unsubscribe is idempotent: detach listener first, then unregister. Handle StrictMode/reconnect/unmount without duplicate listeners. Renderer reload reconstructs state and never restarts a run.

Replay a gap by replacing the subscription with one starting at lastSeq, never retaining both listeners. Clear sends conversation.upsert with historyReset=true; invalidate/reload that history. Delete removes cached messages, steps, approvals, and replay selection for the conversation. Ordinary revision changes such as renaming must not reset history.

### 6.3 Process and security boundaries

```text
Renderer: UI, cached state, read-only noVNC, user intent
  → Preload: contextBridge typed request/subscription, fixed channels
  → Main: trusted sender + schema + limits + deduplication
          AgentCoordinator, provider client, credentials, repository, leases
  → Authenticated local Agent connection + Operator business IPC
  → Python RuntimeOwner: global control, queue, epochs, gateway, lifecycle
  → QEMU: guest pixels and bounded HID input
```

- API Key UI displays configured/unconfigured only. Configure/change invokes a new main-owned native credential-entry adapter; where unavailable, use native file selection and main-side reading. Electron does not already supply a native password-entry dialog. No ordinary renderer password field, clipboard polling, or localStorage secrets. Use OS credential storage/secure encryption; if persistence cannot be secure, support process-only use.
- Main uses an authenticated restricted observe/act client. Never expose Agent credentials, QMP, shell, arbitrary HTTP proxying, or paths. agent_ipc.py is a shared-daemon proxy, not an existing built-in Electron Agent.
- New scoped palette-media protocol resolves main-issued IDs with webContents/MIME/expiry checks, never arbitrary paths/remote proxies. Existing app protocol remains package-only. The existing short-lived display.ticket is a read-only display exception; never log/export/persist it.
- Distinguish provider network access from guest network permission. Before execution, explain that screenshots go to the chosen model. Guest allowNetwork requires actual profile/network isolation when denied. Vision cannot guarantee detection of all guest deletions/network operations; do not claim per-domain enforcement without backend support. Clipboard is off by default and disabled when unsupported; never silently synchronize through noVNC.
- VM driver claims must be recognized by shared RuntimeOwner across desktop processes and MCP; main's map is only a fast check. Approval never expands backend authority.

## 7. Tokens

Map to existing Tailwind semantic variables and remove hardcoded component colors. Status colors are restricted to indicators/badges; danger text is a semantic destructive action, not another accent. All necessary content retains contrast in both themes.

| Token | Light | Dark | Purpose |
| --- | --- | --- | --- |
| --background | #F7F6F5 | #191918 | Warm window background |
| --sidebar | #F1F0EE | #1E1E1C | Navigation |
| --card / --popover | #FFFFFF | #242422 | Workspace/input/floating surfaces |
| --muted | #F1F0EE | #30302D | User bubble/selection |
| --hover | #ECEAE7 | #353531 | Hover |
| --border | #E8E6E3 | #3B3B36 | Decorative1px border |
| --foreground | #252522 | #ECECE8 | Text |
| --muted-foreground | #686862 | #AEAEA5 | Secondary text |
| --placeholder | #74746C | #A3A39A | Placeholder |
| --primary / --ring | #2F6FEB | #82ABFF | Single accent/focus |
| --primary-foreground | #FFFFFF | #111827 | Accent button text |
| --primary-hover | #245BC7 | #A1BFFF | Accent hover |
| --primary-soft | #EAF0FF | #263650 | Accent tint |
| --control-border | #2F6FEB | #82ABFF | Human2px border |
| --status-running | #24804A | #79C993 | Running |
| --status-pending | #936300 | #E5BE68 | Starting/stopping/paused |
| --status-failed | #B33434 | #F09A9A | Failure |
| --status-offline | #73736B | #A5A59A | Stopped/disconnected |
| --danger | #B42318 | #FFAAA2 | Destructive action |
| --danger-foreground | #FFFFFF | #2B1010 | Destructive button text |
| --agent-scrim | rgba(247,246,245,.12) | rgba(0,0,0,.16) | Light display overlay |
| --modal-scrim | rgba(20,20,18,.28) | rgba(0,0,0,.52) | Modal backdrop |

| Category | Values | Rule |
| --- | --- | --- |
| Font | Inter, -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif | Local fonts; system monospace for code |
| Type/line-height | Caption12/18, body13/19.5, chat14/21, title16/24, page20/30 | Weight400 body,500–600 headings/buttons; no necessary11px text |
| Radius | 6,12,20,999px | Controls, cards/composer, workspace, pills |
| Spacing | 4,8,12,16,20,24,32,48px | 4px grid; border1px exception |
| Icons/indicators | 16px icons,32px empty icon,6–8px dots | Stroke1.5–1.75; target32px |
| Focus | 2px ring +2px offset | focus-visible; shared accent |
| Shadows | None on base panels; popup light0 8px 28px #00000014, dark #00000052 | No persistent heavy display/bubble shadow |
| Motion | 150/180/200ms ease-out | Collapse/expand/overlay;0ms under reduced-motion, static icon instead of ripple/rotation |

Theme via root data-theme and prefers-color-scheme, with matching main window background/titlebar overlay and no white flash. Forced-colors strengthens boundaries/focus without removing state text.

## 8. Implementation stages and acceptance

Deliver incrementally behind feature flags. The tests below are requirements, not claims that they have already run.

| Stage | Scope | Acceptance |
| --- | --- | --- |
| P0 Contracts/boundaries | Types/schemas, coordinator state machine, shared driver claim, controlled UI APIs | Vitest illegal methods/VM/payload/state/idempotency; backend cross-client exclusion; no renderer credentials/model calls |
| P1 Shell/routes | Titlebar/rail/navigation/Home/management/Settings/resize/command | Vitest restore/corrupt storage/breakpoints; Playwright1440/1280/1279/1100/zoom1099, all handles/reset/keyboard, Ctrl+B/J, settings restore, no app horizontal overflow |
| P2 Conversations/chat | Repository, VM binding, group/recent/search, drafts/attachments, controlled chat | Vitest pagination/order/archive/draft isolation; Playwright target required, display switch, rename/menu/IME/long code, failure preserves draft, restart retains history |
| P3 Display/human control | Console extraction, toolbar overflow, lifecycle views, main leases/fullscreen/replay | Vitest input order/coordinates/scroll/epochs/fault blocking; Playwright release shortcuts/chords, blur/fullscreen/disconnect,100% scale, replay input blocked, drawer focus |
| P4 Agent/approval | Main visual loop/provider, subscription, policies, handoff | Vitest allow/deny/expiry/duplicates, takeover/approval/action races, late provider result, release failure, competing conversations, event recovery; Playwright full task→approval→takeover→return→completion |
| P5 Settings/recovery | Credential adapter/import, policy, pause negotiation, roots, exit/crash | Vitest capability/policy/secret/log/exit failure; Playwright save/discard, close-only pauses Agent but retains VM, full exit/MCP warning, interrupted recovery without autoexecution |
| P6 Visual/hardware | Paired docs, Chinese copy, themes/accessibility, real QEMU | Theme screenshots; Windows/macOS titlebar; keyboard/screen reader/reduced motion/200% zoom; real mutual exclusion/release/fresh observation/lifecycle with recorded evidence |

| Suggested tests | Required assertions |
| --- | --- |
| desktop/tests/layout.test.ts | Width sums, user/automatic priority, invalid/blocked storage, preserved full widths |
| desktop/tests/agent-store.test.ts | Bootstrap/subscribe gap, duplicate seq/delta offset/gap replay/old epoch/terminal events, StrictMode cleanup |
| desktop/tests/agent-coordinator.test.ts | Atomic claim, no action during approval, no human input before acquire, cancellation, observe before resume |
| desktop/tests/approval.test.ts | Digest/epoch/policy/expiry binding, idempotent decision, deny blocks, takeover invalidates |
| Existing ipc.test.ts/input.test.ts | Trusted frames/scoped media/payload bounds, input ordering and epoch regression |
| Existing shutdown.test.ts/vm-settings.test.ts | Agent stop/release failure plus original shutdown/hardware coverage |
| desktop/e2e/agent-workflow.spec.ts | Controlled provider/captures for complete workflow and actual summary counts |
| desktop/e2e/layout.spec.ts | Electron content width, collapse/drawer/routes/fullscreen/focus/themes/overflow |
| Existing exit/windows-ui/vm-settings specs | Preserve creation/hardware/window/MCP exit regressions |
| Backend unit/operator_ipc/integration | Driver claim, old epochs, release failure, desktop/MCP competition, pause/resume |
| Real QEMU e2e/manual | Actual pointer/key effects, Ctrl+Alt+Del/Super,3-second expiry, host differences |

Run desktop build, Vitest, and Electron Playwright for implementation changes. Backend changes also require pytest, Ruff, and mypy. Validate docs with `uv run python scripts/check_docs.py`. A documentation-only delivery must not claim application regression acceptance.

Release blockers: accepting human input before takeover confirmation; resuming on unconfirmed release; concurrent VM writers; executing a different action than approved; renderer-readable secrets; noVNC write path; replayed actions after event recovery; input on historical screenshots; automatic continuation of a pre-crash plan.

Hardware evidence records host, QEMU/backend version, Profile, guest image, procedure, outcomes, and logs. Mock providers, skipped hardware tests, and screenshots prove UI/contracts only; they do not establish real-QEMU acceptance.
