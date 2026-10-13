# Palette

Electron 桌面应用支持创建虚拟机、保留磁盘修改和实时人工操控。源码运行与 MCP 共用
后台的说明见[桌面应用指南](docs/desktop.zh-CN.md)。

[English](README.md) | **简体中文**

[![CI](https://github.com/KashiwagiEri233/Palette/actions/workflows/ci.yml/badge.svg)](https://github.com/KashiwagiEri233/Palette/actions/workflows/ci.yml)

Palette 为视觉 Agent 提供一个**可观察、可操作、可重置的虚拟桌面**——一台它能看到、能点击的
QEMU 客户机，而不是宿主机桌面。

基线路线：

```
Agent → MCP 服务器 → 受控 QMP 接口 → QEMU 标准 HID 设备 / 显示缓冲区
```

保留的增强路线（共用同一套权限、调度与审计，需要客户机内服务）：

```
Agent → 同一 MCP 服务器 → Guest Tool Bridge → 客户机内增强工具（原生文本、启动程序）
```

Palette 从不向 Agent 暴露原始 QMP、HMP 或通用客户机 Shell。Agent 收到的是图像，发出的是有界的
动作；其余一切——授权、调度、审计——都发生在那条边界之后，无法从工具调用触及。宿主机路径与
QEMU 参数只能通过下面那些**显式开关**到达，绝不会作为其他调用的副作用发生。

## 状态

按已文档化的 MVP 范围实现。服务本体、MCP 工具契约、QMP 客户端、截图、输入、生命周期、权限策略
与客户机增强工具契约均已实现，并有 fake QMP 与真实受监督子进程测试覆盖。Profile 支持原生
Windows/WHPX、macOS/HVF、Linux/KVM，以及显式选择的 x86_64、aarch64 TCG 模拟。
本次变更**没有执行这些平台的真实客户机验收**；模拟器替身测试不能证明桌面兼容性或真实延迟。

## 快速开始

```bash
uv sync
uv run palette doctor                 # 探测宿主、QEMU、加速器与 profile
uv run palette serve                  # 保持运行，或由 MCP 客户端启动它
```

在另一个终端使用相同配置根目录：

```bash
uv run palette vm start               # 使用宿主原生 profile
uv run palette vm status
uv run palette vm stop
```

VM 由 `serve` 持续管理。生命周期 CLI 命令连接正在运行的服务，不会创建临时服务。请先配置匹配的
客户机镜像。默认 profile 选择器是 `auto`；前提条件与完整 profile 名称见[平台指南](docs/platforms.zh-CN.md)。
Apple Silicon 上使用 `--profile tcg-x86_64` 即可模拟 x86_64 客户机，无需 Rosetta。

用户指南、运维手册、客户机模板构建说明与威胁模型见 `docs/`——其中每一页都提供中英两份。**运行前
请先读 `docs/threat-model.zh-CN.md`**：下表有三项会把服务此后无法再约束的边界交给调用方。

## 安全默认值

每一行都是 profile 里的一个开关，不是硬规则。随仓库提供的 `profiles/default.toml` 在每一项上都
取宽松一侧；`profiles/strict-example.toml` 把这些开关全部关掉，除此之外是同一台机器。

| 控制项 | 默认值 | 含义 |
| --- | --- | --- |
| 网络 | 有虚拟网卡，`restricted-lab` | 通过 user-mode networking 附带一块网卡并开启 `restrict=on`，客户机无法路由出沙箱。`nat` 让它拥有与宿主同等的出站能力且没有出站策略；`offline` 则不附带任何网卡。受强制的白名单未实现，会被拒绝 |
| 宿主机材料：镜像 | 允许 | 调用方可以指定基础镜像，绕过 profile 固定的路径与摘要 |
| 宿主机材料：ISO / 额外磁盘 | 允许 | 调用方可以从任意宿主路径挂载 CD-ROM 或磁盘 |
| 宿主机材料：QEMU 参数 | 允许 | 调用方可以追加原始参数，这**彻底取消了宿主边界**：`-chardev`、`-device`、`-blockdev` 能读写宿主任意文件，`-mon`/`-vnc` 会加出第二条驱动客户机的通道 |
| QMP 传输 | QEMU 子进程的私有管道，绝不使用 TCP | 不受上面那些开关影响 |
| Palette 自身的路径 | 由服务在 `.runtime/` 下生成，绝不接受调用方提供 | 不受影响：开关改变的是 QEMU 被指向哪里，不是 Palette 在哪里清理 |
| 客户机增强工具 | 启用 | 在客户机内服务完成握手之前不产生任何效果；对着没有服务的客户机调用会如实报“不可用”，而不是变成键盘模拟 |
| Agent 生命周期 | 仅可启动 | 启动开放；停止、强制停止与重置仍属运维 |
| TCG 回退 | 关闭 | 缺少硬件加速是可诊断的失败。但调用方显式传 `-accel tcg` 依然会生效——参数一旦开放，参数级防护就不再是保证 |
| 进程寿命与内存 | 依宿主平台实现 | Windows 使用 Job Object；macOS/Linux 使用 supervisor 和 250 ms RSS 采样。POSIX 内存预算允许短暂超调 |
| 审计 | 只记动作类型、长度、时序与结果 | 绝不记录输入正文或持久化截图；调用方提供的路径进实例 manifest，不进日志 |
