# 虚拟机增强工具

**简体中文** | [English](guest-enhancements.md)

Palette 复用 QEMU Guest Agent、SPICE vdagent 和 spice-webdavd，并提供 Windows 用户会话助手。没有安装或没有运行这些客户机服务时，基础画面、键盘和鼠标仍可使用。没有响应表示“当前不可用”，不能直接认定未安装。后台会持续检测，在服务恢复和客户机重启后重新识别能力。QGA 继续负责二进制传输、时间同步和关机；Windows 文件管理、PowerShell、进程/注册表、App 与 UIA 使用私有 `org.palette.tools.0` 通道。

## Windows 文件、系统与应用工具

按下文重建并装载 Windows 工具盘，**在客户机内**运行 `Install.cmd`。安装程序使用 Windows 自带的 .NET Framework 编译器编译随盘 C# 源码，无需 Python 或客户机网络。重启虚拟机以连接新增 VirtIO 端口，然后正常登录。`vm_status.enhancements.windows` 报告握手、交互桌面状态和实际授权能力。

`PaletteGuestTools` SYSTEM 服务只转发受限 VirtIO 端口上的消息。独立进程通过只允许当前用户和 SYSTEM、并校验工作进程 PID 的本地管道，以活动控制台用户的普通令牌执行操作，不自动提权。锁屏时拒绝界面、App、文字与剪贴板操作；文件和系统操作仍受登录用户权限约束。仅卸载此助手可在客户机运行 `Install.cmd -Uninstall`，上游驱动及服务保留。

| 内置 Agent | 外部 MCP | 功能 |
| --- | --- | --- |
| `filesystem` | `vm_guest_filesystem` | `read/write/copy/move/delete/list/search/info`；文本读写、追加、目录元数据和有界结果 |
| `powershell` | `vm_guest_powershell` | 客户机 PowerShell 源码、可选 `cwd`、有界标准输出/错误、退出码及超时/截断标记 |
| `process` | `vm_guest_process` | 列出、过滤和排序进程；按 PID 或无歧义名称关闭、强制终止 |
| `registry` | `vm_guest_registry` | `get/set/delete/list`；String、ExpandString、Binary、DWord、QWord、MultiString 类型 |
| `app` | `vm_guest_app` | 本地化开始菜单名称启动、独立 `args`/`cwd` 的可执行文件启动、切换焦点、移动/缩放窗口 |
| `snapshot` | `vm_guest_snapshot` | 有界 Windows UIA 控件、稳定窗口 ID、控件编号、快照 ID 和 QEMU 截图 |
| `multi_select` | `vm_guest_multi_select` | 最多 64 次点击，可保持 Ctrl 多选 |
| `multi_edit` | `vm_guest_multi_edit` | 最多 64 项 Unicode 字段填写；`clear=true` 先全选并删除 |

`filesystem` 相对路径从客户机当前用户桌面解析。文本写入及批量文字合计最多 48 KiB UTF-8；每个字段还受 `guest_tools.max_text_chars` 限制，默认 4096 字符。`read` 的 `offset` 是从 1 开始的行号，目录/搜索则是从 1 开始的结果位置，截断会明确报告。二进制传输继续使用已有 QGA 分块工具。复制/移动拒绝路径重叠，覆盖已有文件需显式 `overwrite=true`，不支持替换已有目录；递归修改不跟随重解析点。删除非空目录需 `recursive=true`。

PowerShell 请求接受 1–60 秒，默认 20 秒；实际时限会在 Profile 的增强请求和动作期限内预留回执/清理时间，并通过 `effective_timeout_seconds` 返回。标准输出和错误各最多 16384 字符，截断后继续排空输出。超时或取消会停止命令作业及其子进程，不撤销已发生的效果。App 启动分离参数，不拼接 Shell 命令，也不把进程启动视为窗口已出现。名称有歧义时失败，请使用可执行路径或快照的 `window_id`。

批量 `locs` 使用返回截图的像素坐标，与 `vm_act` 一致。控件编号方式必须同时提供 `snapshot_id` 和**同一快照的 `frame_id`**，使用 `labels=[编号,...]` 或 `labels=[[编号,文字],...]`。每次点击前重新验证控件；客户机变化、助手重连或超过 30 秒后编号失效。UIA 原生边界会映射到 QEMU 画面，多显示器边界不匹配时报告 `labels_usable=false`，不猜测坐标。`snapshot` 默认最多 500 个控件、上限 2000，并报告截断；本次没有独立浏览器 DOM/Firefox IA2 模式。

所有新增调用沿用认证 Agent IPC、Profile 授权、实例/启动代次、运行声明、单写入者调度及请求去重。批量失败立即停止，返回 `completed/total`、释放按键并截取结果画面，不提供回滚；不能自动重放 `partial/unknown`。剪贴板 get/set 保持原工具名和 48 KiB 文本限制，优先使用已就绪 Windows 助手；仅在尚未发送请求时使用配置好的 SPICE 通道。两条路径都不访问宿主剪贴板。

普通 Profile 在握手成功后默认开放新增能力；显式能力列表和 `strict-example` 继续保持限制，剪贴板还独立受 `guest_desktop.clipboard` 控制。旧 `vm_guest_launch_app(app_id, options)` 保留应用白名单契约，新 `vm_guest_app` 使用独立的 `guest.app` 授权。

自动化测试会编译并运行 Windows 助手的分帧诊断入口，验证 Unicode 文件、PowerShell 输出/时限及临时 HKCU 值。服务安装、VirtIO 访问、登录/注销、真实窗口焦点、UIA 输入和人工接管仍须真实 Windows 虚拟机验收；模拟测试和宿主诊断测试不能替代这些验收。

QWord 注册表值超过 JavaScript 精确整数范围时，请使用十进制字符串；读取也会以十进制字符串返回此类值。Agent 响应元数据上限为 1 MiB，图片使用独立的有界二进制帧；普通操作者消息继续保持 256 KiB 上限。

## 在 Debian / XFCE 客户机内安装

将 `guest/linux/` 复制进虚拟机，然后**在虚拟机内部**运行：

```sh
sudo sh guest/linux/install.sh
```

脚本安装发行版的 `qemu-guest-agent`、`spice-vdagent`、`spice-webdavd`、`x11-xserver-utils`、`python3` 和 `gvfs-backends`，以及无特权的 X11 会话助手和仅针对就绪端口的权限规则。安装后注销并重新登录。在关机状态的硬件设置中将显卡选为 **virtio-gpu**，然后重启虚拟机。VGA 仍可用于基础画面、键鼠和剪贴板，但不提供本方案的自动分辨率。

卸载 Palette 会话助手和自启动项（保留发行版安装的软件包）：

```sh
sudo sh guest/linux/install.sh --uninstall
```

已经运行的助手需要注销后退出。助手仅对虚拟 XRandR 输出应用变化的 virtio-gpu 首选模式，成功查询 X11 画面后才发送就绪心跳；不以 root 运行、不接收命令、不提供 Shell。该助手暂不支持 Wayland 和 Windows 客户机的自动分辨率。Windows 客户机可按上游说明安装 [SPICE 工具](https://www.spice-space.org/download.html)和 [QGA/virtio 驱动](https://virtio-win.github.io/Knowledge-Base/Qemu-ga-win.html)以使用其他适用能力，但需要单独进行客户机验收。

## Windows x64 安装光盘

宿主执行 `uv sync --locked` 后，在仓库构建离线安装光盘：

```sh
uv run python scripts/build_windows_tools.py
```

构建器下载固定版本的官方 [VirtIO 一体化安装包和驱动 ISO](https://github.com/virtio-win/virtio-win-pkg-scripts#downloads) 0.1.285，以及 SPICE WebDAV 2.4，按 `guest/windows/payloads.json` 的大小及 SHA-256 校验，生成 `images/guest-tools/palette-tools-windows-x86_64.iso` 和同名 `.json` 清单。首次构建还需下载约 753 MiB 的上游驱动 ISO；工具盘仅收录选定的 x64 驱动目录，保留 INF、SYS、CAT、配套文件和上游许可证的原始内容，逐文件回读验证后发布。ISO 同时包含安装后使用的脚本、QGA、SPICE Agent 和 WebDAV 服务，客户机无需联网。缓存位于 `.runtime/guest-tools-cache/`，`--offline` 可从已校验缓存重建。更新版本需同时更新固定地址、大小和摘要并重新验收。镜像与缓存不提交到 Git。

独立配置目录使用 `--output <配置目录>/images/guest-tools`，或将 ISO 和 JSON 清单一起复制到该目录。菜单不自动下载文件或执行宿主安装程序；文件缺失或损坏会报错。

选中 Windows 虚拟机，打开顶栏**应用菜单 → 虚拟机 → 安装增强工具与驱动**，或该虚拟机的**更多 → 安装增强工具与驱动**。运行中首次使用时动态新增独立 USB 光驱，后续装载复用它，重复装载同一工具盘不打开托盘。关机状态保存设置并在下次启动时装载。只读 CD-ROM 可由 Windows 10/11 内置 USB 存储驱动读取，保留已有驱动器、安装 ISO 和启动顺序。相同菜单可**弹出增强工具光盘**，只弹出该工具盘介质，运行中的光驱保留供再次使用，不强制弹出被占用介质，也不卸载客户机软件。

### 安装 Windows 时加载驱动

保留 Windows 安装 ISO，并通过上述菜单同时装载工具盘。在“你想将 Windows 安装在哪里？”页面点击**加载驱动程序 → 浏览**，按虚拟机的磁盘控制器选择工具盘中的目录：

| 设备 | Windows 11 x64 驱动目录 |
| --- | --- |
| VirtIO Block 磁盘（`virtio-blk`） | `drivers\viostor\w11\amd64` |
| VirtIO SCSI 磁盘 | `drivers\vioscsi\w11\amd64` |
| VirtIO 网卡 | `drivers\NetKVM\w11\amd64` |
| VirtIO 串口（增强服务通道） | `drivers\vioserial\w11\amd64` |

Windows 10 将 `w11` 换成 `w10`；Server 2016/2019/2022/2025 分别使用 `2k16/2k19/2k22/2k25`。选择对应系统的 `amd64` 目录，保留“隐藏与此计算机硬件不兼容的驱动程序”选项。加载匹配磁盘控制器的驱动后刷新磁盘列表；SATA/IDE 磁盘使用 Windows 内置驱动，无需加载 VirtIO 存储驱动。

Windows 初始设置（OOBE）缺少网络时，可使用界面的驱动加载入口选择 `NetKVM` 目录。没有入口时按 **Shift+F10**，执行下列命令；`E:` 替换为实际工具盘盘符，系统目录也按版本替换：

```bat
pnputil /add-driver E:\drivers\NetKVM\w11\amd64\netkvm.inf /install
```

### 安装完成后安装增强服务

进入 Windows 桌面后打开工具盘运行 **Install.cmd**，确认管理员权限，完成后自行重启。该脚本不用于 Windows Setup / WinPE，检测到 WinPE 时会提示从 `drivers` 加载驱动并退出。脚本验证内置文件并顺序运行上游安装程序，日志保存在 `%ProgramData%\Palette\GuestTools\Logs`。失败停止并提示日志，已安装组件保留供修复；可重跑以重试失败的安装，或通过 Windows 应用列表修复、卸载各组件。不自动重启，不修改驱动签名策略、TPM 或系统安装检查。

当前支持 Windows 10/11、Server 2016 及更新的 **x64** 系统。ARM64 和 Linux 预设不装载此包；自定义配置需自行确认客户机为 Windows x64。剪贴板、Unicode 粘贴、QGA 文件操作和 WebDAV 仍需宿主 Profile 启用对应通道；装载不会改变通道策略。Windows 自动分辨率尚未提供，Linux X11 会话助手不适用于 Windows。装载不代表安装成功，安装成功不代表服务在线，Agent 继续按运行时握手和实际能力判断、降级。

装载和弹出经过共享后台操作锁、配置版本及实例校验；Agent 占用时拒绝操作。QMP 超时或回读无法确认状态会报错，不自动重放或误报完成。更新磁盘上的工具 ISO 后，需重新装载才能更新持久记录。

## 宿主配置

普通内置 Profile 默认启用可选通道，`strict-example` 保持关闭。已有虚拟机保存的硬件选项不会自动改变；自定义 Profile 中这些配置的默认值为关闭：

```toml
[guest_agent]
enabled = true
request_timeout_seconds = 3.0
max_file_bytes = 16777216

[guest_desktop]
clipboard = true
resize = true
shared_folders = true
max_clipboard_bytes = 49152

# 可选的持久共享目录，由可信宿主操作者配置：
[[shared_folders]]
tag = "documents"
path = "exchange"
read_only = true
```

共享目录的相对路径按配置根目录解析。只暴露明确配置或选择的目录。QGA 与 WebDAV 使用每个实例独立的私有通道：POSIX 为 Unix socket，Windows 为命名管道，不在宿主开放管理或文件共享 TCP 端口。升级后需重启共享后台，通道配置变化后需重启虚拟机。Windows 会在 QMP 握手之前连接命名管道，避免 QEMU 管道初始化等待客户端而阻塞。

## Agent 直接操作与自动降级

增强能力直接提供给内置 pi Agent 和外部 MCP 客户端，不需要人工点击“增强工具”、接管或文件选择对话框。内置 Agent 的工具名与外部 MCP 对照如下：

| 内置 Agent | MCP | 功能 |
| --- | --- | --- |
| `input_text` | `vm_guest_input_text` | 设置客户机剪贴板并发送 Ctrl+V，直接输入中文、多行和 Emoji，返回截图 |
| `clipboard_get` / `clipboard_set` | `vm_guest_clipboard_get` / `vm_guest_clipboard_set` | 直接读取/设置客户机文本剪贴板，最多 48 KiB UTF-8，不访问宿主剪贴板 |
| `file_open` / `file_read` / `file_write` / `file_close` | `vm_guest_file_open` / `vm_guest_file_read` / `vm_guest_file_write` / `vm_guest_file_close` | 通过 QGA 分块上传/下载二进制内容，不弹出宿主文件选择器 |
| `display_resize` | `vm_guest_resize` | 请求客户机分辨率并观察实际画面尺寸 |
| `sync_time` | `vm_guest_sync_time` | 将宿主 UTC 时间同步到客户机 |
| `status` | `vm_status` | 查询增强能力、QGA 信息及服务状态 |

先用状态结果取得 `instance_id`、`boot_epoch` 和 `enhancements`；文字输入还要求最新 `frame_id`。例如外部 MCP 的中文输入参数（实例、代次和画面 ID 替换为刚读取的值）：

```json
{"vm_id":"desktop","instance_id":"当前实例ID","boot_epoch":1,"frame_id":"最新画面ID","request_id":"paste-1","text":"中文输入 😀","method":"clipboard_paste"}
```

文字输入将剪贴板设置与 Ctrl+V 放在同一调度事务中，其他写入者不能插入操作。成功只表示已提交，需检查返回截图确认应用反应；终端可能需要先设置剪贴板再显式发送 Ctrl+Shift+V。`native_text` 仍依赖独立自定义服务。缺少增强服务时返回 `GUEST_TOOL_UNAVAILABLE`，保留画面与 ASCII 键盘基线，不把中文静默转成按键；服务恢复后可重新调用。

文件传输：用客户机绝对路径和 `mode="w"` 打开上传（会覆盖客户机文件），从 `data.transfer_id` 取得句柄；将二进制数据编码为 Base64，逐块调用 `file_write`；结束或失败后调用 `file_close`。下载用 `mode="r"`，重复 `file_read` 并解码 `data.data_base64`，直到 `data.eof=true`，然后关闭。每块最多 49152 字节，单文件默认最多 16 MiB，同时最多四个句柄。空文件只需打开后关闭。上传失败可能保留部分客户机文件。QGA 按客户机服务自身的权限访问文件；这些工具不授予宿主文件系统访问权。

每个操作携带独立 `request_id`（内置 Agent 自动生成），相同 ID 和参数只返回原回执，避免重复粘贴、追加或跳过读取块；换参数复用 ID 会被拒绝。结果未知时不自动重放。内置 Agent 会停止任务以便检查。所有调用经过共享输入调度器、人工控制权与 Agent 运行声明检查；人工接管时拒绝 Agent 操作，控制权交接/运行结束会清理文件句柄，重启使旧代次失效。原有手动控制台的“自动分辨率”仍是人工观看时的可选行为，Agent 可直接调用分辨率工具。

桌面安全关机在已知 QGA 在线时使用 QGA，否则沿用 ACPI；超时报错，不自动强制断电，结果未知时不重放 ACPI。

## Windows、macOS、Linux 宿主的共享目录

私有复用协议适配客户机 `spice-webdavd`，宿主文件服务复用 [WsgiDAV](https://github.com/mar10/wsgidav)，无需将显示协议切换为完整 SPICE，也不要求 SMB 或 virtiofs。在运行 spice-webdavd 的客户机文件管理器中打开：

```text
dav://127.0.0.1:9843/
```

共享目录由可信 Profile 的 `[[shared_folders]]` 明确配置，默认只读。Agent 可通过客户机 WebDAV 访问已导出的目录，不能自行选择任意宿主目录。上游文件提供器阻止越界和符号链接逃逸，错误响应不返回宿主文件系统诊断路径。单次请求/响应最多 16 MiB，同时最多四个客户机 HTTP 连接。协议适用于当前的小端 x86_64/aarch64 目标，实际挂载行为仍需按客户机 WebDAV 客户端验收。

## 验证

| 目标 | 已验证范围 |
| --- | --- |
| Windows 宿主、QEMU 11.1、显式 TCG、空白 scratch 磁盘 | 已用真实 QEMU 验证启动、QMP/帧缓冲、全部私有宿主通道、未安装工具时能力禁用，以及进程清理 |
| Windows Electron 控制台 | 原有控制台流程、移除人工增强入口后的基础控制流程已通过测试；内置 pi SDK 模型工具调用另有契约测试 |
| Linux/macOS 宿主 | 已实现 POSIX 通道；当前 Windows 开发环境未完成这些宿主的实机验收 |
| 已安装工具的 Debian/XFCE 客户机 | 已提供安装脚本和会话助手；剪贴板、实时分辨率、QGA 操作与 WebDAV 挂载仍需真实桌面镜像验收 |
| Windows x64 工具盘 | 固定上游包及 ISO 内容已校验；真实 QEMU 11.1 验证独立工具光驱、只读冷/热装载和弹出，原系统光盘保留；Windows 内安装与服务功能尚需验收 |
| Windows/Wayland 客户机 | 不提供本项目的自动分辨率助手；客户机内 QGA/SPICE/WebDAV 能力需要单独验收 |

`palette doctor` 将宿主 virtio-serial、qemu-vdagent 依赖作为非阻断检查。运行时 `vm_status.enhancements` 与操作者 `guest.status` 提供实际可用状态；QGA 在线不代表交互桌面已就绪。

单元和集成测试覆盖服务晚启动、消息分片、超时后的 QGA 重新同步、命令禁用、控制权隔离、关机结果未知、传输大小和资源清理、中文剪贴板与解压限制，以及 WebDAV 只读、中文路径与目录越界。Agent 测试覆盖无人工租约的中文输入、剪贴板与二进制传输、认证 IPC、去重和人工接管；内置 pi SDK 测试驱动模型直接调用工具。Electron 测试检查基础控制。

实机客户机验收单独执行：在真实镜像安装工具，双向复制中文与 Emoji，聚焦/失焦时调整尺寸，比较上传下载文件摘要，挂载并检查只读/可写目录，停止和重启服务，重启客户机后检查能力恢复，以及关机超时时不会自动强杀。模拟测试和仅启动固件的 QEMU 烟雾测试不能作为客户机功能实机通过的依据。

上游资料：[QGA 协议](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html)、[SPICE 共享目录](https://www.spice-space.org/spice-user-manual.html#_folder_sharing)、[QEMU 剪贴板实现](https://github.com/qemu/qemu/blob/master/ui/vnc-clipboard.c)。
