# 虚拟机增强工具

**简体中文** | [English](guest-enhancements.md)

Palette 复用 QEMU Guest Agent、SPICE vdagent 和 spice-webdavd。没有安装或没有运行这些客户机服务时，基础画面、键盘和鼠标仍可使用。没有响应表示“当前不可用”，不能直接认定未安装。后台会持续检测，在服务恢复和客户机重启后重新识别能力。中文输入与剪贴板使用标准 QEMU/SPICE 通道，文件传输使用 QGA；`native_text` 与应用启动仍依赖独立的 `org.palette.tools.0` 自定义协议。

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

构建器下载固定版本的官方 [VirtIO 一体化安装包](https://github.com/virtio-win/virtio-win-guest-tools-installer) 0.1.285 和 SPICE WebDAV 2.4，按 `guest/windows/payloads.json` 的大小及 SHA-256 校验，生成 `images/guest-tools/palette-tools-windows-x86_64.iso` 和同名 `.json` 清单。ISO 内含脚本、VirtIO 驱动、QGA、SPICE Agent 和 WebDAV 服务，安装时无需客户机联网。缓存位于 `.runtime/guest-tools-cache/`，`--offline` 可从已校验缓存重建。更新版本需同时更新固定地址、大小和摘要并重新验收。镜像与缓存不提交到 Git。

独立配置目录使用 `--output <配置目录>/images/guest-tools`，或将 ISO 和 JSON 清单一起复制到该目录。菜单不自动下载文件或执行宿主安装程序；文件缺失或损坏会报错。

选中 Windows 虚拟机，打开顶栏**应用菜单 → 虚拟机 → 安装增强工具与驱动**，或该虚拟机的**更多 → 安装增强工具与驱动**。运行中立即插入只读光盘，关机状态保存设置并在下次启动时装载。工具盘使用独立 USB 光驱，Windows 10/11 可用内置 USB 存储驱动读取，保留原安装 ISO 和启动顺序。升级前已启动的实例需关机后重新启动以添加工具光驱。相同菜单可**弹出增强工具光盘**，不强制弹出被占用介质，也不卸载客户机软件。

在 Windows 客户机打开光驱运行 **Install.cmd**，确认管理员权限，完成后自行重启。脚本验证内置文件并顺序运行上游安装程序，日志保存在 `%ProgramData%\Palette\GuestTools\Logs`。失败停止并提示日志，已安装组件保留供修复；可重跑以重试失败的安装，或通过 Windows 应用列表修复、卸载各组件。不自动重启，不修改驱动签名策略、TPM 或系统安装检查。

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
