# 人工接管低延迟显示

[English](operator-latency.md) | **简体中文**

低延迟模式保留 noVNC 画面和经过控制策略的 QMP 输入，在 VNC 旁启用 QEMU CPU D-Bus 显示后端，不启动原始帧 helper。Agent 观察使用 QMP 权威截图，图像编码移到串行输入/采集队列外执行。

## 设置与降级

在**设置 → 运行环境**启用**低延迟显示**。受鉴权的 `display.settings.get/update` operator 接口持久化本机选择。修改通过虚拟机关闭后重新启动生效，该流程会重建 QEMU 显示配置。设置不会重启运行中的虚拟机，并列出各活动实例的实际显示模式、降级原因及是否需要关闭后重新启动。

显式关闭会恢复原有 VNC 启动路径，并禁用 D-Bus 及其 helper，包括已经配置的原始帧实验。没有本机偏好时沿用现有可信实验配置。低延迟模式检测实际 QEMU 的 D-Bus 支持。不支持时使用兼容路径并报告原因。输入凭据仍位于后端/main process，Agent 不能自行选择此显示设置。

VNC 会在画面空闲时逐步放慢刷新，而 QMP 输入不经过 VNC 的输入消息唤醒路径。QEMU 的全局刷新调度取显示 listener 的最小间隔，D-Bus console 提供默认的 30 ms 间隔。实际帧率取决于客户机与宿主处理能力，空闲 CPU 工作量可能增加。[QEMU 实现](https://github.com/qemu/qemu/blob/master/ui/console.c)

本机 Windows 的隔离、暂停、无磁盘 QEMU 11.1.0 测试完成只读 RFB 握手。`-display none` 的刷新间隔在约六秒内退避到 780 ms。`-display dbus,p2p=yes,gl=off` 在没有 helper 时保持 30 ms。真实桌面拖动、滚动的验收状态单独列于下文。

## 截图顺序

公共观察在现有队列内获取不可变截图字节，并在允许后续输入之前固定原始尺寸、显示/生命周期代次、动作序号、采集时间和内部采集序号。PNG 解码、缩放及缩略图随后在队列外执行。每台虚拟机在入队前最多预留两份待处理截图，慢编码不会造成无界字节积压。短文件 I/O 不等待图像编码 semaphore。

即使后来发生输入，完成的观察仍携带采集时的动作序号，现有动作校验据此拒绝过期画面。Registry 按采集顺序选择和淘汰，慢旧编码不会替代较新的动作后截图。生命周期或分辨率变化会阻止旧画面发布。取消/超时不发布晚到帧。关闭先释放按住的输入，再等待处理安全收尾。动作内部与动作后截图保持现有事务语义。

## 复现真实桌面对比

使用一次性客户机副本，固定宿主、QEMU 版本、加速器、CPU/内存、镜像版本及采集配置。脚本会发送空格、拖拽和滚轮，不启动、停止或修改虚拟机定义。在**客户机浏览器内**打开 `guest/fixtures/display-latency.html`，全屏并聚焦，预先设置实际分辨率。浏览器缩放须为 100%、device pixel ratio 须为 1，使 CSS 标记像素与原始帧像素一致。Chromium/Edge 可加 `--force-device-scale-factor=1` 启动。五条二进制标记都必须可见。存在帧缓冲偏移时使用 `--marker-x/--marker-y`。脚本在接管及计时前等待标记确实绘制，尺寸握手完成不等于画面已就绪。

整个矩阵期间保持一次性客户机唤醒，两种方案使用相同电源设置。关闭显示器自动熄屏/DPMS、屏幕保护、自动锁定和待机。Windows 可**在一次性客户机内**运行 `powercfg /change monitor-timeout-ac 0` 与 `powercfg /change standby-timeout-ac 0`，并配置屏保/锁定。如果客户机已经黑屏，在准备阶段通过正常租约输入恢复，再确认 fixture 可见后开始测量。QMP 截图同样为黑色时，原因可能是客户机 DPMS。

标记就绪且完成接管后，脚本会在 fixture 空白中心点击一次、按住按钮 40 ms，使浏览器获得焦点，再发送一次空格并验证编号反馈。一次性校准记录在 `preparation`，标为 `measured: false`。它位于资源采样前，每轮/负载会清空计时数组。校准失败会停止并保存诊断，不重放结果未知的输入，也避免把启动或电源命令窗口抢占焦点计入延迟。

基准使用构建后的 Desktop。

```sh
npm --prefix desktop run build
```

测量整轮优化时，在另一个终端运行隔离 owner，等待输出 `ready: true`。测试根目录须已有一次性配置及 profiles，`runtime_root` 位于其子目录，并且没有活动 owner。该命令建立独立基准后端。

```sh
uv run python scripts/operator_latency_owner.py --root /absolute/test-root --disposable --observation-pipeline serial
```

`serial` 仅在该进程内隐藏可选的分离采集接口，复现采集和编码一起占用 FIFO 的旧事务，不增加生产配置开关。通过连接该测试根目录的 Desktop 选择兼容显示并启动一次性虚拟机。每种分辨率都需要指定实际 VM ID 和固定客户机镜像版本。

```sh
node desktop/scripts/benchmark-display.mjs --root /absolute/test-root --vm-id VM_ID --provider rfb --variant adaptive --width 1280 --height 800 --guest IMAGE_VERSION --guest-arch x86_64 --accelerator whpx --output .runtime/adaptive-800.json
node desktop/scripts/benchmark-display.mjs --root /absolute/test-root --vm-id VM_ID --provider rfb --variant adaptive --width 1920 --height 1080 --guest IMAGE_VERSION --guest-arch x86_64 --accelerator whpx --output .runtime/adaptive-1080.json
```

完成基线后停止一次性虚拟机，再通过受鉴权 operator 端点停止隔离 owner：

```sh
uv run python -c "import asyncio; from pathlib import Path; from palette.config import load_config; from palette.operator import OperatorClient; asyncio.run(OperatorClient(load_config(Path('/absolute/test-root')).runtime_root).call('runtime.stop'))"
```

重新启动 owner，采用生产分离处理路径：

```sh
uv run python scripts/operator_latency_owner.py --root /absolute/test-root --disposable --observation-pipeline split
```

启用低延迟显示，只启动一次性测试虚拟机，用 `--variant steady` 重复测量，输出 `steady-800.json` 和 `steady-1080.json`。脚本核对实例实际显示模式及观察处理路径，只修改偏好的旧实例无法通过。准备其他分辨率或根目录时，也采用显式停止虚拟机、受鉴权停止 owner、重新启动 owner 的顺序。仅重启客户机不会更换 owner 处理路径或 QEMU 显示参数。

```sh
uv run python scripts/compare_display_reports.py --baseline .runtime/adaptive-800.json .runtime/adaptive-1080.json --candidate .runtime/steady-800.json .runtime/steady-1080.json --output .runtime/operator-latency-comparison.json
```

默认三轮，每轮 100 次编号按键，每个运动阶段 100 次、60 Hz 定速输入，覆盖无观察（`idle`）与单并发权威观察（`observe`）两种负载。这里的 `idle` 仅表示后台负载，另有 **10 秒和 120 秒**的静置来测首个反馈。三轮分别覆盖按键、拖拽和滚轮唤醒。在 `observe` 负载下，静置期间 Agent 观察仍持续。默认每份报告约有 13 分钟静置，四份报告约 52 分钟，另加输入和编码耗时。

`--samples`、`--rounds`、`--motion-hz`、`--idle-seconds` 和 `--wake-samples` 可用于探索。例如 `--samples 5 --rounds 1 --idle-seconds 1` 仅作 smoke，不能通过验收比较器。不得缩减最终协议来换取通过。

报告记录 renderer 实际 DOM 事件时间与对应客户机标记、关联运动延迟、帧间隔、旧目标落后程度和最终目标排空。四像素坐标桶容忍绝对指针量化。终点坐标重复时还要求客户机运动序号更新，避免前一轮画面满足新输入的判定。被输入队列合并或未呈现的中间移动单独计数，不视为最终反馈丢失。滚轮单步校准把宿主刻度对应到客户机累计位移，客户机事件合并不会扭曲计数。ACK 诊断携带批次序号、事件数和 renderer 时间戳。排队时间戳代表队列窗口起点，合并移动后可能早于实际保留事件，不能当作每个 DOM 输入的时间。CPU/RSS 包括后端/QEMU/helper 与 Electron 进程。CPU 是累计进程 CPU 秒数除以单调时间秒数再乘 100，单核满载为 100%，进程组合计可以超过 100%。RSS 为工作集字节求和，可能重复计算共享页。静置资源与活动资源分开。画布读回只证明客户端可见，不代表物理显示器呈现。

## 验收与证据

比较器要求两种分辨率/负载、至少三个独立轮次、至少 100 次输入、已匹配的最终运动目标、完整进程资源及 10/120 秒静置证据。1080p 持续观察下，**拖拽和滚轮的输入至可见 P95 都必须改善至少 20%**。其他已测延迟、帧间隔、排空、活动 CPU 和 RSS 均不得回退超过 10%。空闲 CPU/RSS 增幅单独报告。缺失或不匹配的证据无法通过。

整轮对比为 **adaptive + serial → steady + split**，报告标为 `comparison_scope: display_and_observation`，达标时 `combined_optimization_gate_passed: true`。也支持 split → split，但标为 `display_refresh_only`，不能据此宣称编码移出 FIFO 的收益。观察处理路径是方案差异字段，不放进要求相等的宿主/客户机执行配置。旧 raw 报告仍可读取，保留其原有证据限制。

设置持久化、不支持构建降级、尺寸变化、换手、断线、画布外释放和生命周期取消仍须单独正确性验证。原始帧报告继续使用 `--provider raw --variant raw`，沿用单独的迁移判据，不允许影子帧授权 Agent 动作。

| 平台 | 刷新机制 | 完整真实桌面矩阵 |
| --- | --- | --- |
| Windows x86_64 | 已通过暂停无盘 QEMU trace 验证 | 完整报告通过前未验收 |
| Linux | 本地未验证 | 未验收 |
| macOS | 本地未验证 | 未验收 |

固件、模拟 IPC 和单元测试各自只证明特定行为，不能替代完整桌面矩阵。本机通过验收后启用低延迟模式。显示仍使用 VNC，原始帧 Canvas 实验仍默认关闭。本地原始报告放在 `.runtime/`，不得提交。

## 2026-11-04 本机评估

**结论——低延迟显示保留手动选择，Windows 桌面性能未验收。** 长时间静置测试多次被 Electron 原生焦点变化中断，用户选择先交付优化与未验收报告。本机原有偏好仍为沿用已有配置。隔离测试 VM 和基准 owner 已停止。

真实桌面探针使用隔离的 Windows 11 build 22000.2777 副本，配置为 QEMU 11.1.0、WHPX、q35、virtio-vga、2 vCPU 和 3072 MiB RAM。观察沿用 1280 像素长边限制。这是临时测试配置，原 VM 存储仅作为只读 backing image。

adaptive/serial 的 1920×1080 短测已完成两种负载，每组一轮，含 20 次编号按键和每种运动 20 次派发输入，静置间隔为一秒。下表仅描述该基线短测的小样本结果。

| 负载 | 按键反馈 P95，ms | 拖动匹配数 / 派发数 | 拖动反馈 P95，ms | 滚动匹配数 / 派发数 | 滚动反馈 P95，ms |
| --- | --- | --- | --- | --- | --- |
| 无观察 | 157.0 | 15 / 20 | 93.0 | 12 / 20 | 111.1 |
| 持续观察 | 380.1 | 2 / 20 | 374.8 | 3 / 20 | 297.2 |

后续 100 次输入的基线采样在长时间静置阶段中止。原生诊断记录了窗口失焦、获得焦点和最小化，随后现有规则自动释放控制。另一次诊断因最终拖动反馈未匹配而停止。失败报告保留原始错误及截图，结果未知的输入没有重放。测试页已禁用拖动目标的原生文本选择和触摸平移，基准在按键前校验控制归属，重复终点要求新的运动证据。

steady/split 候选组与 1280×800 矩阵尚未实测，因此 20% 改善、CPU/RSS 增幅 10% 限制及空闲资源增加均待确认。复现脚本保留三轮、100 次输入和 10/120 秒静置协议。运行期间需要保持基准原生窗口前台。脚本临时阻止宿主显示器休眠并记录焦点变化，测量期间仍执行现有焦点与释放策略。

本机原始证据位于 `.runtime/latency-acceptance-20261030/`，其中 `evaluation-20261104.json` 按哈希索引原始报告，包括 `adaptive-smoke-v9.json`、`adaptive-1080-final.json`、`adaptive-1080-v2.json` 与 `idle-control-diagnostic-v3.json`。目录名保留实验开始日期。

| 覆盖项 | Windows | Linux / macOS |
| --- | --- | --- |
| Desktop 构建 | 通过 | 原生构建未验证 |
| 后端及模拟分支 | 2026-11-04 的 143 项定向测试通过 | 平台启动分支具备宿主模拟覆盖 |
| 前端 | 194 项测试和设置 Electron 流程通过 | 原生 Electron 执行未验证 |
| 真实 QEMU | 刷新机制通过，基线桌面短测完成 | 未验证 |
| 完整桌面验收 | 未验收 | 未验收 |

Ruff、格式检查和严格 mypy 通过。2026-10-30 的先前后端全量测试为 1059 项通过、32 项跳过。这些检查覆盖实现与顺序语义，完整真实桌面性能判据仍待验收。
