# 宿主机运维指南（Operator Runbook）

[English](operator.md) | **简体中文**

**Palette** 为视觉 Agent 与桌面端运行受监管的 QEMU 虚拟机。作为宿主机运维人员，你的核心工作是：探测宿主环境、配置可信 Profile、运行共享后台（`daemon` 或 `serve`）、管理虚拟机生命周期，以及在异常时执行接管或紧急停止。

## 1. 安装与宿主环境自检

```bash
uv sync --locked
uv run palette doctor
```

`palette doctor` 会系统性探测宿主与客户机目标架构兼容性、QEMU 可执行文件与版本固定值、设备模型支持（`VGA`/`virtio-gpu-pci`、`qemu-xhci`、`usb-kbd`、`usb-tablet`、`virtio-blk-pci`）、硬件加速器、UEFI 固件、基础镜像以及 `.runtime/` 目录写权限与 `qemu-img`：

* 缺少任何阻断性前提条件时，`doctor` 会以退出码 `3`（`EXIT_PRECONDITION`）报错，绝不用静默回退到 TCG 掩盖问题。
* 加 `--json` 可输出结构化 JSON 报告；加 `--hash-base` 会完整读取基础镜像并核对 SHA-256 摘要。
* 默认 Profile 选择器为 `auto`；原生 Windows (`WHPX`)、macOS (`HVF`)、Linux (`KVM`) 及显式 `TCG` 配置详见[平台与客户机架构指南](platforms.zh-CN.md)。

### 各平台关键前提条件

1. **普通非特权用户运行**：所有平台均应以普通用户身份运行 QEMU。若当前 Shell 已提权（Windows 管理员或 POSIX `root`），`doctor` 会发出 `WARN` 警告。
2. **Windows (`WHPX`)**：安装 x86_64 QEMU（推荐匹配 Profile 固定的版本），在 BIOS/UEFI 中开启硬件虚拟化，并启用 **Windows Hypervisor Platform** 系统功能（存在 `WinHvPlatform.dll` 是必要条件，实际可用性需启动验证）。
3. **macOS (`HVF`)**：通过 Homebrew 安装 `qemu`，Apple Silicon 默认使用 `macos-aarch64`（搭配 EDK2/AAVMF UEFI 固件），Intel Mac 默认使用 `macos-x86_64`。
4. **Linux (`KVM`)**：安装对应架构的 `qemu-system-x86` 或 `qemu-system-arm` 及 `qemu-utils`，并确保当前用户具有读写 `/dev/kvm` 的权限。

## 2. 新宿主机冒烟测试与实机探针

在配置好 QEMU 与基础镜像的新宿主机上，推荐依次运行以下两个脚本：

```bash
# 1. 验证真实 stdio 上的 MCP 会话初始化、工具枚举与畸形帧容错
uv run python scripts/mcp_stdio_smoke.py

# 2. 执行 P0 实机探针：冷启动 VM → 截图 → 点击 → 动作后截图，并输出各阶段延迟
uv run python scripts/p0_probe.py
```

`p0_probe.py` 会将抓取到的画面写入 `.runtime/probe/` 供人工核验；若宿主机未就绪，脚本会附带具体错误码以非零状态退出。

## 3. 可信配置体系（`palette.toml` 与 `profiles/*.toml`）

所有可信配置均存放在配置根目录的 `palette.toml` 与 `profiles/*.toml` 中，可通过以下命令查看合并解析后的完整 JSON 配置：

```bash
uv run palette config show
```

* **严格 Schema 校验**：配置文件不接受任何来自 Agent 的输入。未知配置键会被直接拒绝加载，防止拼写错误静默失效：
  ```toml
  [lifecycle]
  allow_agent_resett = true    # 拒绝加载：防止将 allow_agent_reset 拼错而静默暴露或关闭权限
  ```
* **禁止静默加速器回退**：加载器硬性拒绝 `allow_accelerator_fallback`——缺少硬件加速必须作为明确故障诊断出来，因为软件模拟（TCG）不具备硬件虚拟化的客户机隔离保证。
* **宽松默认 vs. 严格隔离**：
  * `profiles/default.toml` 默认开启虚拟网卡（`restricted-lab`）、客户机增强通道，以及允许调用方指定镜像（`allow_agent_images`）、挂载 ISO/磁盘（`allow_agent_iso`）和追加原始 QEMU 参数（`allow_agent_qemu_args`）。
  * `profiles/strict-example.toml` 是同一台机器的严格锁定版本，将上述开关全部关闭并使用 `offline` 无网卡模式。**在生产或不可信环境运行前，请务必阅读[威胁模型](threat-model.zh-CN.md)**。

### 网络模式（`[network]`）

```toml
[network]
mode = "restricted-lab"          # 可选："offline" | "restricted-lab" | "nat"
# mac = "52:54:00:12:34:56"
# hostfwd = ["tcp:127.0.0.1:2222-:22"]
```

| 网络模式 | 虚拟网卡 | 客户机网络可达范围 |
| --- | --- | --- |
| `offline` | 不挂载网卡 | 完全离线，客户机与外部双向不可达 |
| `restricted-lab` | QEMU user-mode (`restrict=on`) | 仅限 QEMU 内部 slirp 沙箱，客户机无法路由访问宿主机或外部网络 |
| `nat` | QEMU user-mode | 拥有与宿主机同等的出站访问能力，**无出站过滤策略** |
| `internet-allowlist` | —— | 启动时直接拒绝：user-mode NAT 无法强制执行域名/IP 白名单 |

> 配置 `hostfwd` 会将宿主机端口转发至客户机内部，并在实例启动时记入日志与 manifest（桌面端新增转发固定绑定 `127.0.0.1`）。

## 4. 运行模式：共享 `daemon` 与 `serve`

每个 `runtime_root` 同一时刻只允许存在一个运行时拥有者（`RuntimeOwner`，通过 `.operator/owner.lock` 互斥）。Palette 支持两种后台托管方式：

### 方式 A：常驻共享后台 `palette daemon`（推荐）

适合同时使用 **Palette Desktop**、**运维 CLI** 与 **多个 MCP 客户端**的场景：

```bash
uv run palette daemon start           # 后台启动共享 owner（幂等：若已运行则直接返回成功）
uv run palette daemon status          # 查看当前 owner 的 PID、类型、版本与代次
uv run palette daemon stop            # 停止后台并清理所有受管 QEMU 进程
uv run palette daemon run             # 在前台运行 daemon（便于调试或系统服务托管）
```

当共享 `daemon` 运行时：

* 不带标志执行 `uv run palette serve` 会自动发现 `daemon` 并作为轻量 `stdio` 代理连接；MCP 客户端断开时不会停止 `daemon` 或正在运行的虚拟机。
* 传入 `uv run palette serve --connect-only` 可强制要求必须连接已有 owner（若无运行中的 owner 则以 `PRECONDITION_FAILED` 退出）。

### 方式 B：MCP 会话独占后台 `palette serve --standalone`

```bash
uv run palette serve --standalone     # 由当前 stdio 会话独占拥有后台，stdin 关闭时自动停止所有受管 VM
```

* 所有日志均输出至 **`stderr`**，确保 `stdout` 纯净承载 MCP JSON-RPC 协议流。
* 本地运维端点（POSIX Unix Socket / Windows 当前用户 Named Pipe）在 MCP 初始化前即就绪，因此可在另一终端使用相同的 `--root` 执行 `palette vm ...` 运维命令。

## 5. 虚拟机生命周期与紧急控制

在共享 `daemon` 或 `serve` 运行期间，可在另一终端执行以下运维命令（均以受信 `operator` 角色执行，不受 `allow_agent_*` 开关限制）：

```bash
uv run palette vm start  --vm-id desktop
uv run palette vm status --vm-id desktop
uv run palette vm stop   --vm-id desktop
uv run palette vm stop   --vm-id desktop --force
uv run palette vm reset  --vm-id desktop
uv run palette vm list
```

启动时可按需覆盖宿主机材料：

```bash
uv run palette vm start --vm-id scratch     --image D:/images/debian-testing.qcow2     --iso   D:/templates/firmware.iso     --disk  E:/data/scratch.qcow2     --qemu-arg=-smp --qemu-arg=4
```

* **`start`**：基于只读基础镜像创建独立 `overlay.qcow2`（持久桌面 VM 则复用其受管可写层），无窗口启动 QEMU，完成私有管道 QMP 握手，恢复客户机执行，并轮询等待首个有效显示缓冲区。
* **`stop`**：发送 ACPI 关机指令并等待配置的关机预算，随后终止 QEMU；仅当进程确认退出后才返回成功。
* **`reset`**：撤销控制权、取消排队动作、释放按住的输入、停止进程，按保留策略处理旧可写层，然后从只读基础镜像重建全新 `overlay.qcow2` 并以新的 `instance_id` 和递增的 `boot_epoch` 启动。基础镜像永不被修改，也绝不执行 `qemu-img commit`。
* **`list`**：离线读取磁盘上的实例目录、manifest 与占用字节数（无需后台在线）。

### 紧急停止（Emergency Stop）

```bash
uv run palette vm emergency-stop --vm-id desktop
uv run palette vm emergency-stop                  # 省略 --vm-id 则立即停止所有受管 VM
```

当 Agent 点错位置、按键卡死或需要立即冻结现场时使用：立即释放所有按住的输入、取消正在执行的动作并强制终止 QEMU 进程。

### 人工接管控制权（Take Control）

```bash
uv run palette vm take-control --vm-id desktop --reason "human debugging"
```

立即释放当前按住的键鼠输入，递增 `controller_epoch`，并使 Agent 手中所有旧 `frame_id` 全部作废（后续基于旧状态推导的动作均会被拒绝）。若需可视化交互接管，推荐直接使用 [Palette Desktop 控制台](desktop.zh-CN.md)。

## 6. 运行目录结构、审计与保留策略

### `.runtime/` 目录结构

```
<runtime_root>/                      # 默认为 .runtime/（受 PathGuard 严格收敛保护）
├── .operator/
│   ├── endpoint.json                # 本地 IPC 发现端点、服务代次与鉴权 Token（仅当前用户可读）
│   └── owner.lock                   # 单一 RuntimeOwner 互斥锁
├── definitions/                     # Palette Desktop 创建的持久虚拟机定义与受管磁盘
│   └── <vm_id>/
├── vms/<vm_id>/<instance_id>/       # 运行实例目录
│   ├── disk/overlay.qcow2           # 实例可写 qcow2 差量层
│   ├── frames/                      # 截图临时暂存区（交付后即刻删除）
│   ├── logs/                        # 实例运行日志
│   └── manifest.json                # 实例身份、backing chain 与附加材料记录
└── audit/
    └── palette.jsonl                # 滚动审计日志（仅记录元数据）
```

### 审计日志记录原则

`audit/palette.jsonl` 仅记录动作类型、输入长度、耗时、执行结果与错误码。**绝不记录**输入文本正文、启动参数、持久截图或宿主机路径。即使 Agent 在客户机内输入了密码，审计日志中也不会留下密码明文（但请注意，屏幕截图本身已发送给视觉模型）。

### 磁盘与截图保留策略（`[retention]`）

* `keep_overlays = false` 时，在 `stop` 或 `reset` 确认进程退出后删除不再使用的停止层；为 `true` 时保留最近 `max_overlays` 个停止层。
* 若某个历史层仍被其他活动或保留层作为 backing 文件引用，或进程退出尚未确认，服务会强制保留该层以确保安全。
* 截图默认在交付后立即删除（`keep_frames = false` 且 `persist_screenshots = false`）。

## 7. 常见故障排查与恢复

| 症状或错误码 | 原因说明 | 恢复操作 |
| --- | --- | --- |
| `state: FAILED` | QEMU 意外退出或私有 QMP 管理管道断开 | 运行 `palette vm status` 查看具体错误，随后执行 `palette vm reset` |
| `QMP_UNAVAILABLE` | 私有 QMP 管道已断开 | 当前实例已不可恢复；执行 `palette vm stop --force` 后重新 `start` |
| `screen_ready: false` | 尚未采集到有效显示缓冲区 | 客户机可能仍在早期引导阶段；若长时间不就绪，检查固件/显卡配置并重启实例 |
| `desktop_ready: null` | 未经客户机内服务握手确认桌面就绪 | 属于正常预期：服务不猜测桌面登录状态，请通过 `vm_observe` 或桌面端控制台查看画面 |
| 按键或鼠标卡住 | 动作取消或异常后按键未释放 | 调用 `vm_release_inputs`；若管理通道已失联则执行 `palette vm emergency-stop` |
| CLI 报错 `PRECONDITION_FAILED` | 当前 `runtime_root` 没有在线的后台 owner | 先运行 `uv run palette daemon start`（确保 `--root` 一致）；离线查看实例可用 `palette vm list` |
| 启动 `serve` 报错 `STATE_CONFLICT` | 另一个独立服务已持有当前 `runtime_root` 锁 | 连接到已有服务（去掉 `--standalone`），或指定独立的 `runtime_root` |
| CLI 报错 `ACTION_OUTCOME_UNKNOWN` | 命令发出后本地 IPC 连接中断 | 先运行 `palette vm status` 检查状态，切勿盲目重复执行破坏性命令 |
| `ACCELERATION_UNAVAILABLE` | 宿主/客户机架构不匹配或 Hypervisor 未启用 | 开启宿主硬件虚拟化（WHPX/HVF/KVM），或显式指定 `--profile tcg-x86_64` / `tcg-aarch64` |
| 缺少 AArch64 UEFI 固件 | 未能在标准路径自动发现 EDK2/AAVMF 固件 | 安装对应固件包或在 Profile 中显式配置 `qemu.firmware.code` 与 `vars`（见[平台指南](platforms.zh-CN.md)） |
