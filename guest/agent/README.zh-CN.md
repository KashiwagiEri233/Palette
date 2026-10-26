# 客户机内增强服务契约（In-Guest Agent Contract）

[English](README.md) | **简体中文**

本目录定义 Palette **保留的客户机增强通道（Guest Enhancement Channel）** 在客户机内部一侧的服务协议规范。

* **宿主机侧状态（已完整实现）**：长度分帧协议（`src/palette/guest_tools/protocol.py`）、版本与能力交集协商、权限策略检查、`GuestToolBridge`（`src/palette/guest_tools/bridge.py`）以及进程内乱序/断连模拟传输层（`MemoryGuestToolTransport`）均已实现并通过测试覆盖。
* **Windows 客户机实现**：`guest/windows/Palette.*.cs` 实现 SYSTEM 传输代理和普通用户工作进程；工具盘使用 Windows 自带 .NET Framework 编译器离线编译并安装代理服务。真实 `VirtioSerialGuestToolTransport` 使用每个实例独立的私有 socket/命名管道。服务缺失时明确报告不可用，绝不把 Unicode 静默退化为按键。

安装、工具参数、权限、限制与仍需真实虚拟机执行的验收，见[增强工具指南](../../docs/guest-enhancements.zh-CN.md)。

## 1. 传输层规范（Transport）

* **专用私有通道**：通过宿主机按实例私有打开的专用 `virtio-serial` 端口（默认名称 `org.palette.tools.0`）通信。严禁使用监听中的 TCP 网络套接字，也严禁与 QEMU 的 QMP 管理流复用同一管道。
* **长度前缀分帧（Length-Prefixed Framing）**：每条消息由 **4 字节无符号大端整数（`uint32 BE`）** 表示的字节长度，紧跟对应长度的 **UTF-8 JSON** 字节流组成（详见 `palette.guest_tools.protocol`）。
* **双向有界限流**：宿主机与客户机服务在读写两端均强制执行单帧最大字节数与请求频率限制。

## 2. 握手与能力协商（Handshake）

连接建立或客户机发生 `RESET` 后，宿主机与客户机服务必须完成一次显式握手：

1. **宿主机发送 `hello`**：
   引导信封为 `v="1.0"`、`kind="hello"`、`id=connect_id`；载荷包含 `versions=["1.1","1.0"]`、`vm_id`、`instance_id`、`boot_epoch`、唯一 `connect_id` 和 `desired_capabilities`，后续请求使用协商版本。
2. **客户机服务回复 `hello_ack`**：
   包含选定的 `version`、实际实现的 `capabilities`/`input_methods`、已登记的 `apps`、`session="interactive"` 与 `interactive_desktop`。Windows 助手要求 1.1；宿主保留对旧服务 1.0 的支持，此类连接仅开放原有操作。

> **交集原则**：宿主机 Bridge 最终仅启用“Profile 授权集”与“客户机 `hello_ack` 声明集”的**严格交集**。客户机服务绝不能声称自己无法兑现的能力。

## 3. 受控操作契约（Operations）

操作采用显式注册的能力分类。Windows 助手提供原生文字、文件管理、PowerShell、进程、注册表、App、UIA 快照、编号解析及客户机剪贴板。请求包含 `capability`、强类型 `params`、`session`、`boot_epoch`、连接 `generation`、`connect_id`、`timeout_ms`；响应包含 `ok/effect_state/method/detail/data`。新增分类必须同时注册并由 Profile 授权。以下两个接口保留原有契约：

| 能力名称 | MCP 工具对应 | 请求载荷字段 | 执行规则与边界约束 |
| --- | --- | --- | --- |
| `guest.input_text` | `vm_guest_input_text` | `{text, method, target}` | `method` 必须显式指定为 `"native_text"`（通过客户机平台原生输入 API 写入）或 `"clipboard_paste"`（仅设置客户机内部剪贴板并粘贴，绝不触碰宿主机剪贴板）。若客户机不支持所请求的 `method`，必须直接拒绝，**严禁自动换用另一种方法** |
| `guest.launch_app` | `vm_guest_launch_app` | `{app_id, options}` | `app_id` 必须映射到客户机与 Profile 共同登记的固定可执行文件路径及有界选项白名单（如 `text-editor` → `/usr/bin/mousepad`）。**严禁接受自由形式的命令行字符串、任意路径或 `argv` 数组，且严禁通过 Shell（`sh -c` / `cmd /c`）启动进程** |

## 4. 客户机服务不可妥协的安全准则

1. **必须在交互式图形用户会话中运行**：在 Linux/X11 或 Windows 桌面中，服务必须绑定到当前可见的普通用户交互式图形会话（`target_session = "interactive"`）。在后台脱离图形环境的会话中拉起进程不算“打开了桌面应用”。
2. **绝不提权运行**：以客户机普通桌面用户身份运行，绝不自动调用 `sudo`、绝不触发 UAC 提权弹窗、绝不持有任何宿主机凭据。
3. **如实报告效果状态（`effect_state`）**：响应必须明确返回 `effect_state`（`not_sent`、`submitted`、`partial`、`unknown`）、实际执行的 `method` 以及应用启动的 `launch_id`。请注意：进程成功创建仅代表 `submitted`，不代表应用窗口已经渲染在屏幕上——Agent 仍需通过动作后截图确认视觉结果。
4. **双向零信任校验**：校验每条 JSON 和结果上限，原生文字受 `max_text_chars` 限制，默认 4096。文件/系统/App 操作仅在客户机执行并独立授权，SYSTEM 代理只转发消息，不执行这些操作。

超时或取消时，宿主发送 `kind="cancel"`、原请求 ID 和连接身份。代理停止工作进程及命令作业，不重放操作；下次请求先对新进程重新握手。宿主调度与去重保留部分/未知结果，批量键鼠事件仍记入 QMP 输入账本，编号解析本身不发送输入。

## 5. 如何验证自定义客户机服务实现

运行协议、真实编译 Windows 工作进程及服务集成测试：

```bash
# 运行进程内协议、握手交集、超时与乱序回复契约测试
uv run pytest tests/guest_tools -v

# 运行客户机增强通道与 PaletteService 的集成测试
uv run pytest tests/integration/test_guest_channel.py -v
```
