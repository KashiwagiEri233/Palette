# 运维指南

[English](operator.md) | **简体中文**

Palette 为 Agent 运行一台 QEMU 客户机。运维要做的事就是：检查宿主、建一台 VM、盯着它，出问题时
把它停下来。

## 安装

```bash
uv sync
uv run palette doctor
```

`doctor` 是门槛。任何它无法验证的东西都会报成硬失败——最典型的是缺少 QEMU 可执行文件。它从不
把降级模式当作替代方案提供：

```
palette 0.1.0 doctor
  host      : Windows-11-10.0.26200-SP0 (AMD64)
  python    : 3.12.13
  profile   : default (visual_only)
  checks    :
    [FAIL] qemu_binary: not found on PATH or in the search paths
  result    : NOT READY
```

Windows 上的前提条件：

1. QEMU for Windows，x86_64，最好就是 profile 固定的那个版本。
2. 固件里已开启硬件虚拟化，并且已启用 **Windows Hypervisor Platform** 功能（存在
   `WinHvPlatform.dll` 是必要条件，不是充分条件）。
3. 普通用户账户。QEMU 不能以管理员身份运行；当前 shell 若已提权，`doctor` 会给出告警。
4. 一个客户机模板——见 `guest-template.zh-CN.md`。

## 新宿主机上第一件该跑的事

```bash
uv run python scripts/p0_probe.py --profile default
```

启动、截图、点一次、再截图——也就是计划里的 P0 里程碑，并附上计划要求的各项耗时（启动到
running、观察延迟、点击到动作后图像）。它会把画面写到 `.runtime/probe/`，好让你自己看一眼；
宿主没就绪时，它会带着具体原因以非零码退出。

想检查的是传输而不是 VM：

```bash
uv run python scripts/mcp_stdio_smoke.py
```

它会像客户端一样，经真实 stdio 驱动 `palette serve`：初始化会话、列出工具、调用一个工具，然后
发一帧畸形数据，确认会话能挺过去。

## Profile

所有可信配置都在 `palette.toml` 和 `profiles/*.toml` 里。一个 profile 承载了 QEMU 路径与版本
固定值、加速器、基础镜像及其摘要、设备清单、资源配额、网络模式、截图与文本限制、生命周期权限、
保留策略和增强工具设置。

这里没有任何东西来自 Agent。未知键会被拒绝，所以一个拼错的开关会直接报错，而不是悄悄维持默认值：

```toml
[lifecycle]
allow_agent_resett = true    # 拒绝加载：你是想写 allow_agent_reset 吗？
```

只有一项控制不可放宽，加载器会拒绝任何尝试：

* `allow_accelerator_fallback` —— 静默回退到 TCG 会改变威胁模型所依赖的隔离前提。缺少硬件加速
  是一个要诊断出来的失败。

其余全部是开关，而随仓库提供的 `default.toml` 在其中三项上取了宽松一侧：网卡、客户机增强通道，
以及调用方自行提供镜像、CD-ROM 与 QEMU 参数的权利。`strict-example.toml` 是同一台机器、把这些
开关全部关闭。留着宽松那组之前请先读 `threat-model.zh-CN.md`：最后一项取消了宿主边界，而且任何
参数过滤都换不回来。

## 网络模式

```toml
[network]
mode = "restricted-lab"          # 或者 "offline"、"nat"
# mac = "52:54:00:12:34:56"
# hostfwd = ["tcp::2222-:22"]
```

| 模式 | 网卡 | 客户机的可达范围 |
| --- | --- | --- |
| `offline` | 无 | 什么都到不了，也什么都到不了它 |
| `restricted-lab` | user-mode，`restrict=on` | 无法路由出沙箱 |
| `nat` | user-mode | 与宿主同等的出站能力，**没有出站策略** |
| `internet-allowlist` | —— | 启动时被拒绝：白名单需要一个本构建并不具备的网关 |

`hostfwd` 会从宿主机向客户机打一个洞，并在 VM 启动时被记录，因为它抵消了 `restrict=on` 提供的
一部分隔离。

## 生命周期

```bash
uv run palette vm start  --vm-id desktop --profile default
uv run palette vm status --vm-id desktop
uv run palette vm stop   --vm-id desktop
uv run palette vm reset  --vm-id desktop
uv run palette vm list
```

启动时也可以带上宿主机材料，也就是下面这些 CLI 参数的用途：

```bash
uv run palette vm start --vm-id scratch \
    --image D:/images/debian-testing.qcow2 \
    --iso  D:/templates/firmware.iso \
    --disk E:/data/scratch.qcow2 \
    --qemu-arg=-smp --qemu-arg=4
```

`--image` 替换本实例的只读后端存储；可写层仍然创建在 `.runtime/` 下，仍然是重置所丢弃的东西。
`--qemu-arg` 追加在最后，其威力正如其名——请先读 `threat-model.zh-CN.md`。运维总是可以使用这些
参数；以 Agent 身份发起的调用则需要对应的 `allow_agent_*` 开关。

* **start** 从只读基础镜像创建一份新的 overlay，隐藏启动 QEMU，完成 QMP 握手，恢复被暂停的
  客户机，并轮询等待第一个有效显示缓冲区。
* **stop** 先请求客户机正常关机，等待配置的预算，然后结束 QEMU。只有在进程确实消失之后，stop
  才报告成功。
* **reset** 是破坏性的，也是刻意为之：撤销控制权、取消正在执行的动作、释放输入、停止进程、按
  保留策略留下旧的可写层，再创建新 overlay 并以新的实例标识和 boot 代次启动新实例。基础镜像
  永不被写入，也永不使用 `qemu-img commit`。
* **list** 列出磁盘上的实例目录、它们的 manifest 和占用大小。

状态按计划定义流转：`STOPPED → STARTING → RUNNING → STOPPING → STOPPED`，另有 `PAUSED` 与
`FAILED`。非法流转会被报告出来，而不是被抹平。

## 紧急停止

```bash
uv run palette vm emergency-stop --vm-id desktop
uv run palette vm emergency-stop                  # 所有受管 VM
```

立即释放所有按住的输入、取消正在执行的动作、结束 QEMU。当 Agent 正在点击它不该点的地方、某个键
卡住了，或者你只是想让桌面别再变化时，用它。

## 接管控制

```bash
uv run palette vm take-control --vm-id desktop --reason "human debugging"
```

释放输入、让 Agent 仍能据以行动的每一个 frame 全部作废，并递增 `controller_epoch`——于是 Agent
从旧状态推导出的任何东西都会以 `CONTROL_NOT_OWNED` 被拒绝。重新接入控制台时只能用**只读**方式，
并且要有可证明无法写入的机制；普通的可写 VNC viewer 会破坏单写入者保证，而服务无法察觉。

## 提供服务

```bash
uv run palette serve                 # stdio 上的 MCP，服务单个客户端
uv run palette serve --profile other # 换一个默认 profile
```

日志走 **stderr**。在 stdio 上，stdout 属于 MCP 协议，混进一行日志就会破坏会话。

## 文件都在哪

```
<runtime_root>/                      # 默认 .runtime
├── vms/<vm_id>/<instance_id>/
│   ├── disk/overlay.qcow2           # 该实例的可写层
│   ├── frames/                      # 暂存区；每次截图交付后即被清空
│   ├── logs/
│   └── manifest.json                # 这个实例是什么，来自哪里
└── audit/palette.jsonl              # 有界元数据：类型、长度、时序、结果
```

每一次删除都经过 manifest，并被限制在 `runtime_root` 之内。服务拒绝触碰它之外的路径，也拒绝
跟随它内部指向外部的 junction 或 reparse point。

使用测试替身时，overlay 旁边会出现 `overlay.qcow2.log.jsonl` 和 `.behavior.json`；真实运行中
永远不会出现这些文件。

## 审计日志里有什么

动作类型、输入长度、时序、结果和错误码。它**不**保存输入正文、启动参数或截图，也从不保存宿主
机路径。一次输入了密码的运行不会留下密码的痕迹——但请记住，那张截图本身已经送到模型那里了，
而这不是日志能撤销的。

## 保留策略

停下来的实例会被保留，以便取回结果，数量上限是运行中实例之外的 `max_overlays` 个。如果磁盘
增长比保留近期工作更重要，就把它调小。截图默认完全不保留，除非设置了 `capture.keep_frames`
并刻意启用了 `retention.persist_screenshots`。

## 故障恢复

| 症状 | 含义 | 处理 |
| --- | --- | --- |
| `state: FAILED` | QEMU 退出，或管理通道断开 | 用 `vm_status` 看原因，然后 `vm reset` |
| `QMP_UNAVAILABLE` | 私有通道没了 | 该实例已不可用；`vm stop --force` 后重新启动 |
| `screen_ready: false` | 没有有效显示缓冲区 | 客户机可能仍在启动；若持续如此，检查 `vm_stop`/`vm_start` |
| `desktop_ready: null` | 从未被验证 | 属于预期：服务不推断就绪。自己看一张截图 |
| 有键卡住 | 某次按下没有被释放 | `vm_release_inputs`；通道不健康时用 `emergency-stop` |
| 服务进程死了 | 遗留的 QEMU | Windows 上由 job object 收拾；否则 `palette vm emergency-stop` |

## 测试

```bash
uv run pytest                      # 单元、QMP、客户机工具与集成测试
uv run pytest tests/e2e            # 未配置真实 QEMU 与镜像时跳过
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run python scripts/check_docs.py
```

集成测试套件会启动一个真实子进程，经真实管道讲 QMP，因此在不需要 hypervisor 的情况下覆盖了
分帧、资源回收和生命周期行为。文档检查会强制「中英成对」以及所有相对链接可解析。

CI（`.github/workflows/ci.yml`）在每次 push 与 pull request 上跑 **lint job**：ruff、ruff
format、mypy 与文档检查。**测试矩阵**（Ubuntu 与 Windows、Python 3.12 与 3.13，另加真实 stdio
MCP 会话与 `palette doctor` 冒烟检查）挂在 `workflow_dispatch` 的手动入口后面：没人从 Actions
页面主动触发就不会消耗任何额度，要重新挂回 push 只需改一行。矩阵里必须有 Windows，因为 job
object 进程保护、`CREATE_NO_WINDOW` 与 reparse point 检查只在那里执行。

**两个 job 都跑不了实机验收**，那需要真实 QEMU 与客户机镜像，对应测试会跳过。绿色徽章的含义是
「代码与文档自洽」，**不是**「虚拟机可用」——后者要在真实宿主上跑 P0 探针。
