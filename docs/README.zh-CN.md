# Palette 文档

[English](README.md) | **简体中文**

本目录各页是实用文档：Agent 可以调用什么、怎么运行宿主机、怎么构建镜像，以及什么被刻意排除在
范围之外。有两条规则贯穿全部内容——Agent 只拿到图像和有界的动作，别的一概没有；而视觉基线必须
能独立工作。

| 文档 | 什么时候读 |
| --- | --- |
| [`../README.zh-CN.md`](../README.zh-CN.md) · [English](../README.md) | 想要一段话的总览和安全默认值 |
| [`user-guide.zh-CN.md`](user-guide.zh-CN.md) · [English](user-guide.md) | 你是 Agent 或客户端作者：工具契约、工作循环、各种限制 |
| [`operator.zh-CN.md`](operator.zh-CN.md) · [English](operator.md) | 你负责宿主机：安装、profile、生命周期、紧急停止、故障恢复 |
| [`platforms.zh-CN.md`](platforms.zh-CN.md) · [English](platforms.md) | 你选择 Windows/macOS/Linux、原生客户机或显式 TCG、固件与资源限制 |
| [`guest-template.zh-CN.md`](guest-template.zh-CN.md) · [English](guest-template.md) | 你正在构建 profile 指向的桌面镜像 |
| [`threat-model.zh-CN.md`](threat-model.zh-CN.md) · [English](threat-model.md) | 你需要知道什么可信、什么有强制、什么不在范围内 |
| [`benchmarks.zh-CN.md`](benchmarks.zh-CN.md) · [English](benchmarks.md) | 你要做性能测量，或想知道到底有没有测过 |
| [`known-limitations.zh-CN.md`](known-limitations.zh-CN.md) · [English](known-limitations.md) | 你想看一份诚实的、目前还不能用的清单 |
| [`../guest/agent/README.zh-CN.md`](../guest/agent/README.zh-CN.md) · [English](../guest/agent/README.md) | 你要实现客户机内的增强服务 |

本目录每一页都有中英两份；两个版本靠人工保持同步——只改其中一份，这份改动就不算完成。

## 代码位置

| 路径 | 内容 |
| --- | --- |
| `src/palette/server.py` | MCP 工具契约：schema、图像、结构化结果 |
| `src/palette/service.py` | 权限、调度、审计与生命周期集中在一处 |
| `src/palette/policy.py` | 路径收敛、生命周期授权、能力开关 |
| `src/palette/models.py` | Frame、封闭的动作联合类型、回执、错误体 |
| `src/palette/qmp/` | 私有管理通道：传输与协议 |
| `src/palette/vm/` | 进程、镜像、overlay、生命周期状态机、进程保护 |
| `src/palette/capture/` | 显示缓冲区采集与 PNG/PPM 回退 |
| `src/palette/input/` | 坐标换算、键码表、有界动作事务 |
| `src/palette/guest_tools/` | 保留的增强通道：协议、disabled 后端、bridge |
| `scripts/` | 运维工具（`p0_probe.py`、`mcp_stdio_smoke.py`）与 `check_docs.py` |
| `guest/` | 离线夹具页面与客户机服务契约 |
| `profiles/` | 不含秘密的可信配置模板 |

## 关于测试

`tests/` 按“这里的失败意味着什么”来划分：

* `tests/unit` —— 纯逻辑：坐标、键码表、模型、权限、采集决策，以及针对进程内替身的
  动作事务；
* `tests/qmp` —— 协议层，对着一个可编排的进程内 QMP 对端；
* `tests/guest_tools` —— 增强契约，对着一个 mock 客户机服务；
* `tests/integration` —— 真实子进程经真实管道讲 QMP，外加通过 SDK 自身工具管理器驱动的
  MCP 工具面；
* `tests/e2e` —— 实机验收，未配置真实 QEMU 与镜像时跳过。
