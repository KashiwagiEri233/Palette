# 已知限制与范围边界（Known Limitations）

[English](known-limitations.md) | **简体中文**

本页列出的每一条都是当前版本**刻意划定的架构边界**或**真实存在的技术约束**。Palette 在运行时对这些边界保持完全诚实——不支持的能力会直接以明确错误码拒绝，绝不静默降级或伪装成功。

## 1. 宿主环境与平台约束

* **不分发客户机系统镜像**：仓库仅提供可信配置模板（`profiles/*.toml`），只读基础镜像或安装 ISO 由运维人员自行准备并记录来源与 SHA-256 摘要（见[客户机模板指南](guest-template.zh-CN.md)）。
* **硬件加速绝不自动回退到 TCG**：原生 Profile 支持 Windows (`WHPX`)、macOS Intel / Apple Silicon (`HVF`) 与 Linux x86_64 / aarch64 (`KVM`)；跨架构软件模拟需显式选择 `tcg-x86_64` 或 `tcg-aarch64`。缺少硬件加速会直接报错 `ACCELERATION_UNAVAILABLE`。
* **POSIX 内存上限为采样式限制**：Windows 使用内核级 Job Object 硬性限制内存；macOS 与 Linux 由独立 Supervisor 每 **250 ms** 采样 QEMU 进程树 RSS 总量并在超限时终止进程组，因此允许极短暂的瞬时超调。此外，进程组清理无法约束在客户机之外主动调用 `setsid` 脱离会话的宿主程序（当关闭 `allow_agent_qemu_args` 时，QEMU 自身不会创建此类程序）。
* **实机兼容性需按目标宿主与镜像单独验证**：默认 CI 运行受监督的 fake QEMU 子进程与本地真实管道/RFB 测试，用于验证协议、策略与界面状态机，不替代目标宿主上的实机桌面验收（实机验收流程与 macOS ARM64 实测记录见[端到端验收测试指南](acceptance-test.zh-CN.md)）。

## 2. 桌面交互与设备能力限制

* **纯视觉模式不支持直接输入非 ASCII / 中文文本**：基线 `vm_act` 的 `type_text`（`mode: "keyboard"`）仅发送 **US 布局可打印 ASCII** 扫描码，控制字符（如 `
`、`	`）与非 ASCII 字符会在发送第一个按键前被全量预校验拦截（返回 `UNSUPPORTED_TEXT`）。在桌面端人工接管控制台时，物理按键同样按客户机键盘布局发送扫描码，不直接传输宿主机输入法（IME）合成词组。
* **客户机内增强服务（`guest/agent/`）仅完成宿主机侧契约与 Bridge**：宿主机侧的协议分帧、版本协商、权限开关与 `GuestToolBridge` 已完整实现并经过 mock 契约测试，但客户机内部的 `virtio-serial` 守护进程尚未内置实现。因此在客户机实际运行该服务前，调用 `vm_guest_input_text` 或 `vm_guest_launch_app` 会如实返回 `GUEST_TOOL_UNAVAILABLE`。
* **`type_text` 逐键限速**：每个字符包含按下与释放消息并按 `char_interval_ms`（默认 12 ms）限速发送，256 个字符需要数百次往返，不与单次鼠标点击共用延迟预算。
* **仅支持垂直滚轮（`up` / `down`）**：水平滚动未在标准测试矩阵中验证，因此不在 `vm_act` 的 `scroll` 动作中暴露。
* **无多点触控、手势或压感输入**：指针采用绝对坐标定位（`usb-tablet`），不支持触摸屏手势或相对鼠标锁定（Pointer Lock）。
* **无宿主机剪贴板互通**：为防止数据意外跨边界泄漏，Palette Desktop 与 MCP 均不桥接宿主机剪贴板。
* **单向音频输出、无麦克风采集与设备直通**：桌面端硬件设置支持通过 **Intel HDA** 将客户机声音输出到宿主机默认音频设备（需 QEMU 对应后端与客户机驱动支持），但**绝不采集宿主机麦克风**，也不支持摄像头、USB 设备直通、PCI 直通、多显示器或 `9p`/`virtiofs`/SMB 共享文件夹。
* **`vm_wait` 的变化与稳定检测是像素启发式**：`changed` 与 `stable` 模式基于缩略图比较像素差异，闪烁的光标、系统时钟或动画均可能影响结果，不等同于“客户机应用程序已空闲”。

## 3. 状态、协议与正确性边界

* **开启 `allow_agent_qemu_args` 会取消宿主机边界**：允许调用方追加原始 QEMU 参数等同于允许读写宿主机文件或开启第二条控制通道（详见[威胁模型](threat-model.zh-CN.md)）。在需要严格隔离的环境中，请务必将其设为 `false`（如 `profiles/strict-example.toml`）。
* **像素校验无法替代业务层语义确认**：`frame_id` 校验保证点击落在你所见截图的准确像素上，但网页或窗口可能在两张外表相同的截图之间移动按钮位置。涉及付款、删除或提交表单时，调用方必须实施自己的业务确认策略。
* **幂等去重仅限单次服务进程生命周期内**：`request_id` 去重表保存在当前服务进程的有界内存中。若服务在发出输入后、记录回执前崩溃或断连，会返回 `unknown`（`ACTION_OUTCOME_UNKNOWN`）而非自动重放。
* **私有匿名 QMP 管道不可跨服务重启恢复**：当 `daemon` 或 `serve` 进程退出时，Windows Job Object 或 POSIX Supervisor 会自动清理其管理的 QEMU 子进程；重启服务后需重新启动虚拟机实例（持久虚拟机的磁盘与 UEFI 变量在重启后依然完整保留）。
* **不观测客户机硬件光标位置**：除非当前服务自己在本次实例中移动过指针，否则 `cursor_position_source` 始终报告为 `not_observed`。
* **`screendump` 非事务性原子快照**：QMP 帧缓冲导出在客户机高帧率刷新时可能出现轻微画面撕裂。

## 4. 网络与运维边界

* **仅支持本地传输，无远程多租户 API**：MCP 通过 `stdio` 通信，运维与桌面端 IPC 仅绑定本机 Unix Socket 或当前用户 Named Pipe，实时画面网关仅绑定 `127.0.0.1`。不支持远程未加密 HTTP 或多租户权限隔离。
* **不内置跨 VM 任务编排器**：单个服务可同时管理多台虚拟机（每台拥有独立串行队列与控制权租约），但跨多台虚拟机的任务分发与负载均衡由上层调用方负责。
* **不支持在 QEMU user-mode 网络上伪造出站白名单**：网络模式支持 `offline`、`restricted-lab`（`restrict=on`）与 `nat`（端口转发固定仅监听 `127.0.0.1`）；若配置 `internet-allowlist` 会在启动时被直接拒绝，因为slirp user-mode NAT 无法可靠强制执行域名或 IP 白名单。
