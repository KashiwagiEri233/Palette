# 桌面应用

[English](desktop.md) | **简体中文**

Palette Desktop 管理本机 QEMU 虚拟机，并与 MCP 客户端共用后台。支持导入 qcow2/raw
镜像、从 ISO 安装、保留磁盘修改、连续查看画面，以及独占的人工键鼠操控。

## 从源码运行

安装 Node.js 22.12 或更新版本、uv、Python 3.12/3.13，以及匹配客户机架构的 QEMU。
在 Palette 仓库目录运行：

```bash
uv sync --locked
npm --prefix desktop ci
npm --prefix desktop run dev
```

构建和本地预览：

```bash
npm --prefix desktop run build
npm --prefix desktop run preview
```

首版不捆绑 Python 或 QEMU。启动器始终从当前仓库执行 Python 模块，显式指定 uv
项目；环境设置中选择的配置目录只作为配置来源。uv 不在 PATH 时，可用 `PALETTE_UV`
指定其绝对路径。`PALETTE_ROOT` 可指定初始配置目录。

## 创建和管理虚拟机

选择镜像或 ISO、匹配的架构与 profile、CPU、内存、磁盘容量及网络模式。导入镜像作为
只读基础盘使用，请保留原路径及其 backing chain。容量不能小于基础盘；增大虚拟容量
不会自动扩展客户机分区。固件和磁盘驱动必须匹配客户机，Windows ISO 安装可能需要
安装介质提供 VirtIO 驱动。

定义保存在 `<runtime>/definitions`。磁盘和 UEFI 变量在关机和创建新进程实例后继续
保留。关机后可修改名称、CPU、内存、网络和 ISO 挂载状态；系统安装完成后可移除 ISO。
现有 CLI 创建的虚拟机继续使用临时桌面行为；CLI 生命周期命令操作已有桌面定义时，
会遵循其持久磁盘配置。

重启保留磁盘。**恢复初始状态**会丢弃修改：镜像来源回到原始基础盘，ISO 来源回到
空白盘。删除只清理受管理且没有依赖的文件，不删除导入的原始镜像。GUI 正常关机
在 ACPI 请求超时时报错并保留运行状态；强制停止需要单独确认。关闭窗口时可以选择
后台继续运行、全部关机并退出或取消。

## 控制台与人工操控

打开控制台时默认仅查看。点击“接管”取得该 VM 的控制租约，再点击画面聚焦。持续
按键、拖拽、点击和滚轮都进入 Agent 使用的同一个控制器。物理按键使用客户机布局，
首版不直接传输宿主输入法正文或剪贴板。工具栏可以发送 Ctrl+Alt+Del 和 Super。

Ctrl+Alt+G 释放控制。离开控制台、失焦、隐藏窗口、客户机分辨率变化或断线同样释放
键鼠；心跳丢失三秒后租约自动过期。人工控制期间，Agent 仍可观察，但不能写入。
输入结果未知时不自动重放。

画面网关只监听本机，使用一次性显示票据并过滤 RFB 协议。即使修改 noVNC 的只读
标志，也不能通过显示连接输入。QEMU 凭据留在后台，运维凭据留在 Electron 主进程。
Agent 截图仍使用 QMP，保留原有的帧校验行为。

## 共用后台与 MCP

```bash
uv run palette daemon start
uv run palette daemon status
uv run palette serve --connect-only
uv run palette daemon stop
```

`daemon run` 在前台运行。GUI 优先连接已有 owner，否则启动 daemon。`serve` 发现兼容
owner 时作为代理；没有 owner 时继续使用原来的 stdio 生命周期。`serve --standalone`
明确要求自己拥有后台。代理断开不停止 daemon。GUI 连接 stdio 托管的服务时会显示该
模式，MCP 会话结束仍会停止这些 VM。停止 daemon 会清理所有受管理进程；需要客户机
正常关机时，先在 GUI 中关机。更新后台代码后需要重启 owner。

## 验证

“状态”页每 2 秒更新运行状态、实例、控制权、操作序号、排队任务及保持的键鼠输入。
MCP 连接和实际点击的验收步骤见[完整测试流程](acceptance-test.zh-CN.md)。

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python scripts/check_docs.py
npm --prefix desktop test
npm --prefix desktop run test:e2e
```

Electron 集成测试使用独立配置目录、受监督的 fake QEMU 子进程和真实的本机 RFB
连接，验证传输与界面行为，不证明客户机兼容性、硬件加速或真实桌面延迟。实机验收
仍需要真实 QEMU 和匹配镜像，参见[平台指南](platforms.zh-CN.md)与
[运维指南](operator.zh-CN.md)。
