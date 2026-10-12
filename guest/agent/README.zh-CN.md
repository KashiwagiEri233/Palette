# 客户机工具服务（尚未实现）

[English](README.md) | **简体中文**

本目录是保留的增强通道背后那个客户机内服务将来的所在位置。**本构建中不存在任何实现。** 宿主机
一侧是完整的——协议、分帧、版本协商、能力对齐、权限策略和一个 mock——而
`VirtioSerialGuestToolTransport` 会报告“尚未实现”，而不是假装已经连上。

计划的立场是刻意为之：增强路线现在就定义好，配一个 disabled 后端和 mock 契约测试，这样以后把
它打开是去实现一个传输层，而不是改动架构。它同时被明确地**不**缩减为一个剪贴板 broker，也被
明确地**不**做成通用客户机 shell。

## 一个服务必须满足什么

### 传输

* 经由一个由宿主机私有打开的、按实例绑定的通道访问。计划的首选是专用 virtio-serial 端口
  （`org.palette.tools.0`）；它不能是监听中的 TCP socket，也不能与 QEMU 的 QMP 流混在一起。
* 长度分帧消息：4 字节大端长度，后接 UTF-8 JSON，与 `palette.guest_tools.protocol` 一致。
* 双向都有大小与频率限制。

### 握手

1. 宿主机发送 `hello`，带上它的协议版本、`vm_id`、`instance_id`、`boot_epoch`、一个
   `connect_id`，以及它打算使用的能力。
2. 服务以 `hello_ack` 回复：它选定的版本、它实际提供的能力与输入方法、它登记的应用、它接入的
   会话，以及交互桌面是否就绪。

双方都必须报告“被请求的”与“能做到的”的**交集**。一个宣称自己无法兑现的能力的服务，比一个只
提供更少能力的服务更糟。

### 操作

| 能力 | 请求 | 说明 |
| --- | --- | --- |
| `guest.input_text` | `{text, method, target}` | `method` 为 `native_text` 或 `clipboard_paste`；服务必须拒绝它并不提供的方法，而不是换一种顶上 |
| `guest.launch_app` | `{app_id, options}` | `app_id` 解析为一个固定的可执行文件和一组有界的选项。绝不接受自由形式的命令行、路径或 argv 数组，也绝不使用 shell |

### 不可协商的规则

* **在交互用户会话中运行。** 在 Linux 上要绑定到图形会话的环境。在错误的会话里创建进程，不叫
  “打开了一个应用”。
* **绝不提权。** 不自动 `sudo`，不弹出 UAC，不继承宿主秘密。
* **如实报告。** 返回 `effect_state`（`not_sent`、`submitted`、`partial`、`unknown`）、实际
  使用的方法，以及启动操作的 `launch_id`。请求被接受不等于任务已完成：进程存在并不意味着窗口
  可见。
* **反方向也要把宿主机当作不可信。** 该服务是客户机内部一个很小的攻击面；它应当校验收到的
  一切，并且除上面两项能力之外，不暴露任何文件、shell 或进程管理操作。

## 如何测试将来的实现

把 `VirtioSerialGuestToolTransport` 换成真实传输层，然后运行：

```bash
uv run pytest tests/guest_tools -v        # 契约测试，进程内
uv run pytest tests/integration/test_guest_channel.py -v
```

`MemoryGuestToolTransport` 实现的接口与真实通道相同，并且可以停顿、断开或乱序回复，因此 bridge
的超时、关联与代次逻辑，早已按照真实服务会施加的方式被检验过了。
