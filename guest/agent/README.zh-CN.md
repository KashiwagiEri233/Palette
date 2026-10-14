# 客户机内增强服务契约（In-Guest Agent Contract）

[English](README.md) | **简体中文**

本目录定义 Palette **保留的客户机增强通道（Guest Enhancement Channel）** 在客户机内部一侧的服务协议规范。

* **宿主机侧状态（已完整实现）**：长度分帧协议（`src/palette/guest_tools/protocol.py`）、版本与能力交集协商、权限策略检查、`GuestToolBridge`（`src/palette/guest_tools/bridge.py`）以及进程内乱序/断连模拟传输层（`MemoryGuestToolTransport`）均已实现并通过测试覆盖。
* **客户机侧状态（契约定义，默认未内置守护进程）**：在客户机内部实际部署并运行实现本契约的服务之前，宿主机侧的 `VirtioSerialGuestToolTransport` 会诚实报告通道未连接，`vm_status` 会将传输与服务就绪状态分开报告，任何针对 `vm_guest_input_text` 或 `vm_guest_launch_app` 的调用都会以 `GUEST_TOOL_UNAVAILABLE` 明确失败，**绝不静默退化为模拟键盘按键**。

这一架构设计确保：未来在客户机镜像中启用原生 Unicode/中文文本输入与白名单应用启动时，只需编写一个遵循本协议的轻量级客户机守护进程并接入 `virtio-serial` 端口，无需改动宿主机侧的权限、调度或审计架构。

## 1. 传输层规范（Transport）

* **专用私有通道**：通过宿主机按实例私有打开的专用 `virtio-serial` 端口（默认名称 `org.palette.tools.0`）通信。严禁使用监听中的 TCP 网络套接字，也严禁与 QEMU 的 QMP 管理流复用同一管道。
* **长度前缀分帧（Length-Prefixed Framing）**：每条消息由 **4 字节无符号大端整数（`uint32 BE`）** 表示的字节长度，紧跟对应长度的 **UTF-8 JSON** 字节流组成（详见 `palette.guest_tools.protocol`）。
* **双向有界限流**：宿主机与客户机服务在读写两端均强制执行单帧最大字节数与请求频率限制。

## 2. 握手与能力协商（Handshake）

连接建立或客户机发生 `RESET` 后，宿主机与客户机服务必须完成一次显式握手：

1. **宿主机发送 `hello`**：
   包含宿主机支持的协议版本（`protocol_version`，当前为 `"1.0"`）、`vm_id`、`instance_id`、`boot_epoch`、唯一连接标识 `connect_id`，以及当前 Profile 允许启用的能力列表（`capabilities`）与输入方法（`input_methods`）。
2. **客户机服务回复 `hello_ack`**：
   包含协商选定的协议版本、客户机服务**实际支持**的能力列表（`capabilities`）、实际支持的输入方法（`input_methods`）、已登记可启动的应用 ID 列表（`allowed_apps`）、当前绑定的会话类型（如 `"interactive"`），以及交互式桌面是否已就绪（`desktop_ready: true/false`）。

> **交集原则**：宿主机 Bridge 最终仅启用“Profile 授权集”与“客户机 `hello_ack` 声明集”的**严格交集**。客户机服务绝不能声称自己无法兑现的能力。

## 3. 受控操作契约（Operations）

客户机服务仅允许实现以下两项受控操作，**严禁暴露通用 Shell、任意命令执行、任意文件读写或进程管理接口**：

| 能力名称 | MCP 工具对应 | 请求载荷字段 | 执行规则与边界约束 |
| --- | --- | --- | --- |
| `guest.input_text` | `vm_guest_input_text` | `{text, method, target}` | `method` 必须显式指定为 `"native_text"`（通过客户机平台原生输入 API 写入）或 `"clipboard_paste"`（仅设置客户机内部剪贴板并粘贴，绝不触碰宿主机剪贴板）。若客户机不支持所请求的 `method`，必须直接拒绝，**严禁自动换用另一种方法** |
| `guest.launch_app` | `vm_guest_launch_app` | `{app_id, options}` | `app_id` 必须映射到客户机与 Profile 共同登记的固定可执行文件路径及有界选项白名单（如 `text-editor` → `/usr/bin/mousepad`）。**严禁接受自由形式的命令行字符串、任意路径或 `argv` 数组，且严禁通过 Shell（`sh -c` / `cmd /c`）启动进程** |

## 4. 客户机服务不可妥协的安全准则

1. **必须在交互式图形用户会话中运行**：在 Linux/X11 或 Windows 桌面中，服务必须绑定到当前可见的普通用户交互式图形会话（`target_session = "interactive"`）。在后台脱离图形环境的会话中拉起进程不算“打开了桌面应用”。
2. **绝不提权运行**：以客户机普通桌面用户身份运行，绝不自动调用 `sudo`、绝不触发 UAC 提权弹窗、绝不持有任何宿主机凭据。
3. **如实报告效果状态（`effect_state`）**：响应必须明确返回 `effect_state`（`not_sent`、`submitted`、`partial`、`unknown`）、实际执行的 `method` 以及应用启动的 `launch_id`。请注意：进程成功创建仅代表 `submitted`，不代表应用窗口已经渲染在屏幕上——Agent 仍需通过动作后截图确认视觉结果。
4. **双向零信任校验**：客户机服务同样应将来自通道的每条 JSON 消息视为不可信输入，严格校验字段类型、文本长度上限（`max_text_chars`，默认 4096）与选项键名。

## 5. 如何验证自定义客户机服务实现

当你实现了客户机端服务并补齐宿主机 `VirtioSerialGuestToolTransport` 的管道连接后，可运行以下测试套件验证协议兼容性：

```bash
# 运行进程内协议、握手交集、超时与乱序回复契约测试
uv run pytest tests/guest_tools -v

# 运行客户机增强通道与 PaletteService 的集成测试
uv run pytest tests/integration/test_guest_channel.py -v
```
