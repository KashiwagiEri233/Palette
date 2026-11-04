# Desktop 实验显示方案

[English](display-experiment.md) | **简体中文**

实验让 Desktop 和影子 PNG 截图共享宿主侧 QEMU CPU 帧缓冲，**默认关闭**。生产默认仍为 VNC；Agent 权威帧、坐标检查和动作后截图仍使用 QMP。本阶段结论是保留 VNC，待完整的真实桌面矩阵通过后再评估迁移。

## 构建与启用

`native/display-bridge/` 中的 helper 需要 C11 编译器、GLib/GIO 2.66+、Meson 和 Ninja。Linux 还需要 `gio-unix-2.0` 开发包。`qemu-system-* -display help` 必须列出 `dbus`。

```sh
meson setup .runtime/display-build native/display-bridge
meson compile -C .runtime/display-build
```

Debian/Ubuntu 使用 `build-essential libglib2.0-dev meson ninja-build pkg-config`；macOS 在原生 Homebrew 环境安装 `glib meson ninja pkgconf`。Windows 在 MSYS2 UCRT64 环境安装 `mingw-w64-ucrt-x86_64-gcc`、`mingw-w64-ucrt-x86_64-glib2`、`mingw-w64-ucrt-x86_64-meson`、`mingw-w64-ucrt-x86_64-ninja` 和 `mingw-w64-ucrt-x86_64-pkgconf`。将匹配的 GLib 运行库目录加入后端 PATH，或把所需 DLL 放在 helper 旁。这些是宿主构建依赖，客户机无需新增组件。

在 `palette.toml` 中添加**可信宿主配置**，替换为 helper 的绝对路径：

```toml
[host]
experimental_display = "dbus"
experimental_display_helper = "G:/Workspace/Palette/.runtime/display-build/palette-display-bridge.exe"
```

先停止一次性测试虚拟机，再重启后端加载配置；新启动的 Desktop 虚拟机使用该设置。改为 `experimental_display = "off"` 并重启后端和虚拟机，即恢复默认启动路径。独立的[人工接管显示设置](operator-latency.zh-CN.md)优先于本实验：显式兼容模式禁用 D-Bus/helper，低延迟模式使用 D-Bus 加 VNC、不启动 helper；没有本机偏好时沿用可信实验配置。不引入磁盘转换或 Agent 自选传输能力。

缺少 helper 或 QEMU 能力时回退到 VNC。helper 故障会断开原始帧查看连接并释放控制；显式重新连接会启动新的 helper 和源代次。视频模式切换时，连接会在取得完整表面前断开，需要重新连接画面；不会把未知像素补成看似有效的截图。

## 架构与接口

- 受监督的 C/GIO helper 订阅 `org.qemu.Display1.Listener` 的 CPU `Scanout/Update`、光标及尺寸事件，不声明 Map/GPU 能力。当前支持 32 位 RGB/BGR；不支持的格式回退 VNC。
- 仅 runtime 传递套接字并发送 QMP。Windows 使用 `get-win32-socket`；POSIX 实验模式把唯一的 stdio QMP 改为私有 AF_UNIX，支持 `getfd`/SCM_RIGHTS。普通 POSIX 启动仍使用 stdio，没有新增第二个管理通道。
- `FrameSource` 检查边界、合成增量并发布完整不可变 RGBA 图像。每个查看者只保留一个待发完整帧；渲染回合后的八字节大端序号 ACK 才允许发送下一帧，其他客户消息会被拒绝。慢渲染不会无限堆积完整图像。VNC 的增量字节流不丢弃。
- `display.ticket` 增加 `provider`（`rfb/raw`）、`generation` 和 `instance_id`。没有 `provider` 的旧结果继续按 VNC 处理。单次票据、来源检查和私有 VNC 凭据继续生效。
- 仅 operator 可用的 `display.shadow` 要求 `instance_id`，返回编码时间、尺寸、源序号/年龄和 `authoritative: false`。它不返回 `frame_id`，不能授权动作，也不是 Agent 工具。`experimental-display-v1` 宣告这些诊断能力。
- `control.metrics.resources` 增加后端进程树各进程的累计 CPU 秒数和 RSS，以及不完整标记。`execution` 记录实际 QEMU 版本、CPU/内存、机器/显示设备和采集限制，配置不同的报告不能通过对比。RSS 求和可能重复计算共享页，比较时必须采用同一口径。输入租约、代次、顺序与未知结果不重放规则保持原语义。

helper 依据 [QEMU 官方测试](https://github.com/qemu/qemu/blob/master/tests/qtest/dbus-display-test.c) 和 [D-Bus 契约](https://www.qemu.org/docs/master/interop/dbus-display.html) 实现。本轮不宣称 GPU 零拷贝或远程流媒体收益。

## 复现验证

运行应用测试和显式真实 QEMU 固件检查：

```sh
uv run python -m pytest tests/unit/test_frame_source.py tests/unit/test_display_benchmarks.py tests/integration/test_raw_display.py
npm --prefix desktop run build
npm --prefix desktop test
```

```powershell
$env:PALETTE_DBUS_QEMU = 'C:\Program Files\qemu\qemu-system-x86_64.exe'
$env:PALETTE_DBUS_HELPER = 'G:\Workspace\Palette\.runtime\display-build\palette-display-bridge.exe'
$env:PALETTE_DBUS_ACCEL = 'whpx' # 或显式 tcg
uv run python -m pytest tests/e2e/test_dbus_display.py --basetemp=.runtime/dbus-acceptance
cd desktop
npm exec -- playwright test e2e/raw-display.spec.ts e2e/human-input.spec.ts
```

固件测试验证与 QMP 逐字节相同的像素、启动模式变化、helper 崩溃/重启、按住输入释放及基础镜像保持不变。Electron 测试检查原始画布显示和画布外拖拽释放。停机固件客户机无法证明应用收到输入或真实桌面性能。

仅观察微基准默认三轮、每轮每条路径 100 次：

```powershell
uv run python scripts/display_probe.py --firmware --accelerator whpx --qemu $env:PALETTE_DBUS_QEMU --helper $env:PALETTE_DBUS_HELPER --output .runtime/display-observation.json
```

不使用 `--firmware` 时，通过 `--root` 和 `--profile` 提供已配置的一次性客户机镜像。探针创建独立 runtime，报告实际分辨率，并明确将输入至可见反馈和渲染指标留空。两条采集路径同时启用，其资源数据不属于生产 A/B 对比。

完整 Desktop 测量需要两个独立测试根目录：默认 VNC 与实验 D-Bus，使用相同客户机镜像和资源。启动一次性虚拟机，在**客户机浏览器内**打开 `guest/fixtures/display-latency.html`，进入全屏并聚焦。将实际客户机分辨率分别设为 1280×800 和 1920×1080。不要使用生产客户机，测试会发送空格、拖拽和滚轮。

从仓库根目录运行，每种提供方和分辨率各执行一次：

```sh
node desktop/scripts/benchmark-display.mjs --root /absolute/test-root --vm-id VM_ID --provider raw --width 1920 --height 1080 --guest IMAGE_VERSION --guest-arch x86_64 --accelerator whpx --output .runtime/raw-1080.json
```

基线使用 `--provider rfb --variant adaptive`，本实验使用 `--provider raw --variant raw`。客户机全屏浏览器存在偏移时，用 `--marker-x/--marker-y` 指定标记在原始帧中的坐标。默认在无观察和单并发持续权威观察两种负载下，各运行三轮、每轮 100 次编号输入，另采集定速、坐标/累计位移关联的拖拽/滚动反馈，以及独立的 10/120 秒静置唤醒。新版 fixture 有五条标记；精确指标、耗时和 smoke 参数见[人工接管基准协议](operator-latency.zh-CN.md)。影子编码在输入/资源测量窗口外采样。画布可见不等于物理显示器呈现。产物保存时序和元信息，不保存截图或客户机输入内容。

```sh
uv run python scripts/compare_display_reports.py --baseline .runtime/rfb-800.json .runtime/rfb-1080.json --candidate .runtime/raw-800.json .runtime/raw-1080.json --output .runtime/display-comparison.json
```

矩阵缺失或执行元信息不同不能通过。原始帧实验门槛是 1080p 观察负载下反馈或影子观察 P95 改善至少 20%；反馈、输入 ACK、权威观察、运动帧间隔、关联运动延迟、排空、唤醒反馈、活动总 CPU 和 RSS 均不得回退超过 10%。同时检查两种分辨率/负载、三个独立轮次及样本量。资源测量期间进程成员变化会使对比无效。旧 raw 报告保留原测量能力，不能证明稳定刷新验收。通过仅代表可以进一步评审迁移，影子帧替代权威采集仍需单独的正确性设计。

## 已记录证据：2026-10-30

Windows x86_64、QEMU 11.1.0（`v11.1.0-12130-ge470268ff4`）的 WHPX 和 TCG 固件验收均通过。本机构建使用 GLib 2.90.1、Meson 1.12.1 和 Zig 0.15.2，C 编译无警告。Zig 链接器不接受 Meson 的 `--fatal-warnings`，本地链接需使用 `-Dwerror=false`。不提交预编译 helper 或第三方 DLL。

仅观察 WHPX 微基准，**实际为 720×400 停机固件画面**，交替采样顺序：

| 轮次 | 每条路径样本量 | QMP 观察 P95 | 影子 PNG P95 |
| --- | --- | --- | --- |
| 1 | 100 | 54.255 ms | 6.209 ms |
| 2 | 100 | 55.937 ms | 5.619 ms |
| 3 | 100 | 58.696 ms | 5.904 ms |

本地原始样本保存在 `.runtime/display-whpx-observation.json`。这些数字**不能证明** 1080p 桌面反馈改善：权威观察包括帧注册，影子采集语义较弱，没有测量 renderer，两条路径同时启用。配置中的 1280×800 不等于固件实际尺寸，报告使用测得的分辨率。

| 宿主 | 原生构建 | 真实 QEMU 固件 | 完整桌面性能矩阵 |
| --- | --- | --- | --- |
| Windows x86_64 | 已验证 | WHPX + TCG 已验证 | 未验收 |
| Linux | 本地未验证 | 未验证 | 未验收 |
| macOS | 本地未验证 | 未验证 | 未验收 |

**结论：保留默认 VNC。** 本机已证明共享 CPU 帧源可行，影子截图显示出优化潜力。生产迁移仍需要完整的客户机桌面反馈、资源、跨平台和长稳证据。
