# Palette 文档中心

[English](README.md) | **简体中文**

本目录提供 Palette 的完整技术与操作文档，涵盖 **Palette Desktop 桌面工作台**、**MCP 视觉 Agent 工具契约**、**宿主机共享后台与 CLI 运维**、**跨平台硬件加速**以及**安全威胁模型**。

贯穿全套文档的核心设计原则只有两条：

1. **严格的代理边界**：Agent 绝不接触原始 QMP、HMP、宿主机桌面或宿主 Shell。可选 Windows 增强命令只在客户机以登录用户权限执行。
2. **视觉基线自给自足**：不依赖任何客户机内代理，仅凭标准帧缓冲截图与标准 HID 键鼠事件即可独立完成全部视觉操作闭环。

## 文档导航

| 文档 | 适用读者与核心内容 |
| --- | --- |
| [虚拟机增强工具](guest-enhancements.zh-CN.md) · [English](guest-enhancements.md) | 客户机安装、能力检测与降级、中文剪贴板、自动分辨率、QGA 管理、文件传输与私有 WebDAV 共享 |
| [项目总览 (../README.zh-CN.md)](../README.zh-CN.md) · [English](../README.md) | 架构全景、核心特性、快速开始与安全默认开关速览 |
| [桌面应用指南 (desktop.zh-CN.md)](desktop.zh-CN.md) · [English](desktop.md) | Palette Desktop 源码运行、持久虚拟机创建、离线硬件配置、只读画面网关、人工独占接管与共用后台 |
| [后端资源所有权与并发](backend-architecture.zh-CN.md) · [English](backend-architecture.md) | 应用职责、按 VM 并发、磁盘引用与取消后的资源清理 |
| [桌面 GUI 与内置 Agent 设计规范 (desktop-agent-design.zh-CN.md)](desktop-agent-design.zh-CN.md) · [English](desktop-agent-design.md) | 待实现的四区布局、组件状态、接管与审批时序、类型化 IPC 草案、主题 token 和分阶段验收 |
| [Agent 与客户端用户指南 (user-guide.zh-CN.md)](user-guide.zh-CN.md) · [English](user-guide.md) | 面向 Agent 与 MCP 客户端开发者：10 个 MCP 工具契约、`vm_observe → vm_act` 工作循环、帧代次防抖、文本规则与错误码恢复 |
| [宿主机运维指南 (operator.zh-CN.md)](operator.zh-CN.md) · [English](operator.md) | 面向宿主机管理员：环境检查、`daemon` 与 `serve` 协同、Profile 配置、生命周期控制、紧急停止、保留策略与故障恢复 |
| [平台与客户机架构 (platforms.zh-CN.md)](platforms.zh-CN.md) · [English](platforms.md) | Windows (`WHPX`)、macOS (`HVF`)、Linux (`KVM`) 原生加速矩阵、显式跨架构 `TCG`、UEFI 固件发现与进程内存限制 |
| [客户机模板构建 (guest-template.zh-CN.md)](guest-template.zh-CN.md) · [English](guest-template.md) | Debian 13 + XFCE 标准只读基础镜像构建规范、环境固化与 SHA-256 摘要锁定 |
| [安全威胁模型 (threat-model.zh-CN.md)](threat-model.zh-CN.md) · [English](threat-model.md) | 信任边界划分、调用方材料开关（`allow_agent_*`）的安全后果、服务端强制保证与明确不在防护范围内的事项 |
| [端到端验收测试 (acceptance-test.zh-CN.md)](acceptance-test.zh-CN.md) · [English](acceptance-test.md) | 从共享 `daemon` 启动、GUI 创建 VM、MCP 观察与点击、人工接管冲突到硬件设置变更的完整实机验收流程与实测记录 |
| [性能基准测试 (benchmarks.zh-CN.md)](benchmarks.zh-CN.md) · [English](benchmarks.md) | 截图延迟、单击闭环耗时与冷启动阶段拆分目标、开销分解及物理正确性指标 |
| [已知限制 (known-limitations.zh-CN.md)](known-limitations.zh-CN.md) · [English](known-limitations.md) | 当前版本在输入法、剪贴板、网络白名单、进程内去重、多显示器等方面的真实边界清单 |
| [客户机内增强服务契约 (../guest/agent/README.zh-CN.md)](../guest/agent/README.zh-CN.md) · [English](../guest/agent/README.md) | 客户机内 `virtio-serial` 增强服务的长度分帧协议、能力握手与安全约束 |
| [镜像目录说明 (../images/README.zh-CN.md)](../images/README.zh-CN.md) · [English](../images/README.md) | 只读基础镜像存放规范与 `palette doctor` 校验机制 |

> **双语同步约定**：仓库内所有用户文档均提供中英双语配对版本（`*.md` 与 `*.zh-CN.md`），并由 `scripts/check_docs.py` 在 CI 中强制校验语言切换器、相对链接、代码块闭合与 UTF-8 编码。

## 代码模块映射

| 路径 | 职责与核心内容 |
| --- | --- |
| `src/palette/cli.py` | 运维 CLI 入口：`doctor`、`serve`、`daemon`、`config` 与 `vm` 生命周期命令 |
| `src/palette/server.py` | MCP 工具表面：工具注册、输入输出 Schema、`ImageContent` 与结构化错误封装 |
| `src/palette/service.py` / `contracts.py` / `lifecycle.py` | 本地/远程业务接口、观察与动作编排及统一生命周期规则 |
| `src/palette/runtime.py` | 共享 `RuntimeOwner`：统一托管 `OperatorServer`、`DesktopManager`、`DisplayGateway` 与 MCP 远程代理 |
| `src/palette/desktop.py` / `definitions.py` / `persistent_storage.py` | 持久化 VM 设置、定义原子存储、磁盘扩容与恢复 |
| `src/palette/vm_settings.py` | 强类型虚拟机硬件配置模型（CPU/内存/磁盘/启动/显示/网络/音频）与本机 QEMU 能力探测 |
| `src/palette/display.py` | 仅监听本机的 WebSocket RFB 只读画面网关：一次性显示票据验证与客户端写入消息过滤 |
| `src/palette/control.py` | 人机独占控制权租约管理：3 秒心跳续期、`controller_epoch` 演进与输入释放联动 |
| `src/palette/agent_ipc.py` | 共享后台发现与 `RemoteService` 代理，支持 `palette serve` 连接已有 `daemon` |
| `src/palette/operator/` | 本地运维 IPC 协议与传输实现（Windows 当前用户 Named Pipe / POSIX Unix Socket） |
| `src/palette/policy.py` | `PathGuard` 路径收敛检查、生命周期角色授权与 Profile 能力开关校验 |
| `src/palette/models.py` | `Frame`、封闭的 `Action` 联合类型、动作回执 `Receipt` 与结构化错误载荷模型 |
| `src/palette/qmp/` | 私有管道 QMP 协议客户端、事件分发与命令白名单控制 |
| `src/palette/vm/` | QEMU 命令构建、只读基础镜像与 `overlay.qcow2` 管理、生命周期状态机与宿主进程保护 |
| `src/palette/capture/` | 帧缓冲截图采集、分辨率一致性复核与 PNG/PPM 自动回退 |
| `src/palette/input/` | 图像坐标空间换算、US 物理键码表与有界动作事务控制器 |
| `src/palette/guest_tools/` | 客户机内增强通道协议、握手校验、disabled 后端与 `GuestToolBridge` |
| `desktop/` | Palette Desktop 桌面端源码：`src/main/`（Electron 主进程）、`src/preload/`（类型化桥接）、`src/renderer/`（React UI 与 noVNC 控制台） |
| `profiles/` | 按宿主平台、客户机架构与安全姿态预置的可信 Profile 模板（`*.toml`） |
| `scripts/` | 实机探针（`p0_probe.py`）、MCP 传输测试（`mcp_stdio_smoke.py`）、点击验收脚本（`mcp_click_test.py`）与文档检查器（`check_docs.py`） |

## 测试体系分层

后端测试位于 `tests/`，前端与桌面端测试位于 `desktop/tests/` 和 `desktop/e2e/`：

* **`tests/unit/`**：纯逻辑与进程内替身测试，覆盖坐标换算、键码表、数据模型、权限策略、硬件设置校验、显示网关过滤与动作事务。
* **`tests/qmp/`**：QMP 协议层测试，针对可编排的进程内 QMP 对端验证分帧、超时、断线与事件乱序。
* **`tests/guest_tools/`**：客户机增强通道契约测试，针对 mock 传输验证握手交集、超时与状态回执。
* **`tests/operator_ipc/`**：本地运维 IPC 协议、载荷上限与 Windows Named Pipe 适配器测试。
* **`tests/integration/`**：启动真实受监督子进程并通过真实管道或 AF_UNIX 套接字运行 QMP，验证 `daemon`、持久桌面定义、MCP 工具面、进程回收与故障恢复。
* **`tests/e2e/`**：真实 QEMU 硬件加速与客户机镜像验收测试（默认跳过，需设置 `PALETTE_E2E=1` 启用）。
* **`desktop/tests/` & `desktop/e2e/`**：使用 Vitest 验证前端状态与 IPC 逻辑，使用 Playwright 驱动真实 Electron 窗口验证虚拟机创建、硬件设置、控制台与安全退出流程。
