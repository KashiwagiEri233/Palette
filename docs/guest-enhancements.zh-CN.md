# 虚拟机增强工具

**简体中文** | [English](guest-enhancements.md)

Palette 复用 QEMU Guest Agent、SPICE vdagent 和 spice-webdavd。没有安装或没有运行这些客户机服务时，基础画面、键盘和鼠标仍可使用。没有响应表示“当前不可用”，不能直接认定未安装。后台会持续检测，在服务恢复和客户机重启后重新识别能力。原有 `org.palette.tools.0` MCP 文本与应用启动工具使用独立协议，安装 QGA 不会自动实现该协议。

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

## 桌面操作与自动降级

在虚拟机控制台打开 **增强工具**。面板每三秒自动更新状态。

| 功能 | 行为 | 不可用时 |
| --- | --- | --- |
| 剪贴板 | 按钮明确触发双向文本复制，或发送中文、多行和 Emoji；单次最多 48 KiB UTF-8 | 禁用入口，保留物理键盘。发送后接管并在客户机按 Ctrl+V 粘贴 |
| 自动分辨率 | 勾选 **自动分辨率**，人工接管时防抖请求尺寸；实际尺寸匹配后释放旧输入、同步画面坐标并在窗口仍聚焦时重新接管 | 保留客户机原分辨率与画面缩放；未确认变化时关闭该选项 |
| 客户机信息 | agent 版本、系统、主机名、网络接口 IP | 仍显示 QEMU 生命周期，就绪状态与客户机字段分别报告 |
| 时间同步 | 显式发送宿主 UTC 时间，要求客户机启用对应 QGA 命令 | 禁用按钮，客户机继续自行计时 |
| 桌面安全关机 | 已知在线时使用 QGA，否则沿用 ACPI | 超时报错，不自动强制断电；QGA 结果未知时不重放 ACPI |
| 文件传输 | main 进程选择宿主文件，输入客户机绝对路径；48 KiB 分块，单文件最多 16 MiB；提供进度和取消 | 缺少所需 QGA 命令时禁用入口 |
| 共享目录 | 默认只读，通过客户机 WebDAV 访问用户选择的宿主目录 | 服务不可用时标记状态，基础虚拟机不受影响 |

每次显式增强操作会获取人工控制权，经过同一输入调度器，结束后释放控制权。宿主剪贴板和文件对话框仅在 Electron main 内处理，窗口失焦时取消传输。下载先写宿主临时文件，完整传输并关闭客户机文件后才替换目标文件。上传会覆盖客户机目标路径，取消或断线可能留下部分文件。结果未知的写入、剪贴板操作和关机不会自动重放。

## Windows、macOS、Linux 宿主的共享目录

私有复用协议适配客户机 `spice-webdavd`，宿主文件服务复用 [WsgiDAV](https://github.com/mar10/wsgidav)，无需将显示协议切换为完整 SPICE，也不要求 SMB 或 virtiofs。在运行 spice-webdavd 的客户机文件管理器中打开：

```text
dav://127.0.0.1:9843/
```

点击 **选择共享目录** 选择宿主目录，**只读共享** 默认开启，需要客户机写入时才取消。GUI 选择会替换当前共享集合，持续到虚拟机关机；下次启动恢复 Profile 中的 `[[shared_folders]]` 配置。**停止共享** 清空当前共享。上游文件提供器阻止越界和符号链接逃逸，错误响应不返回宿主文件系统诊断路径。单次请求/响应最多 16 MiB，同时最多四个客户机 HTTP 连接。协议适用于当前的小端 x86_64/aarch64 目标，实际挂载行为仍需按客户机 WebDAV 客户端验收。

## 验证

| 目标 | 已验证范围 |
| --- | --- |
| Windows 宿主、QEMU 11.1、显式 TCG、空白 scratch 磁盘 | 已用真实 QEMU 验证启动、QMP/帧缓冲、全部私有宿主通道、未安装工具时能力禁用，以及进程清理 |
| Windows Electron 控制台 | 原有控制台流程与新增增强工具不可用面板/基础控制流程已通过测试，Playwright 已生成截图 |
| Linux/macOS 宿主 | 已实现 POSIX 通道；当前 Windows 开发环境未完成这些宿主的实机验收 |
| 已安装工具的 Debian/XFCE 客户机 | 已提供安装脚本和会话助手；剪贴板、实时分辨率、QGA 操作与 WebDAV 挂载仍需真实桌面镜像验收 |
| Windows/Wayland 客户机 | 不提供本项目的自动分辨率助手；上游 QGA/SPICE/WebDAV 能力需要单独验收 |

`palette doctor` 将宿主 virtio-serial、qemu-vdagent 依赖作为非阻断检查。运行时 `vm_status.enhancements` 与操作者 `guest.status` 提供实际可用状态；QGA 在线不代表交互桌面已就绪。

单元和集成测试覆盖服务晚启动、消息分片、超时后的 QGA 重新同步、命令禁用、控制权隔离、关机结果未知、传输大小和资源清理、中文剪贴板与解压限制，以及 WebDAV 只读、中文路径与目录越界。Electron 测试检查能力不可用时的面板和基础控制。

实机客户机验收单独执行：在真实镜像安装工具，双向复制中文与 Emoji，聚焦/失焦时调整尺寸，比较上传下载文件摘要，挂载并检查只读/可写目录，停止和重启服务，重启客户机后检查能力恢复，以及关机超时时不会自动强杀。模拟测试和仅启动固件的 QEMU 烟雾测试不能作为客户机功能实机通过的依据。

上游资料：[QGA 协议](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html)、[SPICE 共享目录](https://www.spice-space.org/spice-user-manual.html#_folder_sharing)、[QEMU 剪贴板实现](https://github.com/qemu/qemu/blob/master/ui/vnc-clipboard.c)。
