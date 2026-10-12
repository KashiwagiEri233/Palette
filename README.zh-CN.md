# Palette

[English](README.md) | **简体中文**

Palette 为视觉 Agent 提供一个**可观察、可操作、可重置的虚拟桌面**——一台它能看到、能点击的
QEMU 客户机，而不是宿主机桌面。

基线路线：

```
Agent → MCP 服务器 → 受控 QMP 接口 → QEMU 标准 HID 设备 / 显示缓冲区
```

保留的增强路线（共用同一套权限、调度与审计，默认关闭）：

```
Agent → 同一 MCP 服务器 → Guest Tool Bridge → 客户机内增强工具（原生文本、启动程序）
```

Palette 从不向 Agent 暴露原始 QMP、HMP、宿主机路径或通用客户机 Shell。Agent 收到的是图像，
发出的是有界的动作；其余一切——授权、调度、审计——都发生在那条边界之后，无法从工具调用触及。

## 状态

按已文档化的 MVP 范围实现。服务本体、MCP 工具契约、QMP 客户端、截图、输入、生命周期、权限策略
与客户机增强工具契约均已实现，并有针对 fake QMP 服务的测试覆盖。

**未声称**完成实机验收（WHPX + 真实 Debian 桌面镜像）：本机没有 QEMU 可执行文件，因此
`palette doctor` 会明确报告缺失，而不会静默回退到 TCG。

## 快速开始

```bash
uv sync
uv run palette doctor                 # 探测宿主、QEMU、加速器与 profile
uv run palette vm start --profile default
uv run palette serve                  # 在 stdio 上运行 MCP 服务器
```

用户指南、运维手册、客户机模板构建说明与威胁模型见 `docs/`——其中每一页都提供中英两份。

## 安全默认值

| 控制项 | 默认值 |
| --- | --- |
| 网络 | 离线，无虚拟网卡 |
| QMP 传输 | QEMU 子进程的私有管道，绝不使用 TCP |
| 宿主机路径 | 由服务在 `.runtime/` 下生成，绝不接受 Agent 提供的路径 |
| 客户机增强工具 | 关闭 |
| 任意镜像 / QEMU 参数 / ISO | 拒绝 |
| TCG 回退 | 关闭；缺少硬件加速是可诊断的失败，而非降级 |
| 审计 | 只记动作类型、长度、时序与结果——绝不记录输入正文或持久化截图 |
