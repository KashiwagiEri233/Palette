# 平台、客户机架构与资源限制

[English](platforms.md) | **简体中文**

**Palette** 默认选择与宿主机架构匹配的原生硬件加速客户机。配置中的 `qemu.arch` 描述的是**客户机指令集架构（ISA）**，而 QEMU 可执行文件本身应始终使用宿主机原生架构构建。

> 本仓库不分发客户机系统镜像；请准备与目标架构、固件模式及设备驱动匹配的镜像文件（构建规范见[客户机模板指南](guest-template.zh-CN.md)）。

## 1. 原生硬件加速 Profile 矩阵

在 `palette.toml` 中，默认配置 `host.default_profile = "auto"` 会根据当前宿主操作系统与 CPU 架构按下表自动映射原生 Profile。若宿主机缺少对应的硬件加速器或架构不兼容，Palette 会直接诊断报错，**绝不静默回退到软件模拟（TCG）**。

| 宿主机平台 | 客户机 ISA | 默认 Profile ID | 硬件加速器 | 机器模型与默认显示设备 |
| --- | --- | --- | --- | --- |
| Windows x86_64 | `x86_64` | `default` | `whpx` | `q35` + `VGA` |
| macOS Apple Silicon | `aarch64` | `macos-aarch64` | `hvf` | `virt` + `virtio-gpu-pci` + UEFI |
| macOS Intel | `x86_64` | `macos-x86_64` | `hvf` | `q35` + `VGA` |
| Linux x86_64 | `x86_64` | `linux-x86_64` | `kvm` | `q35` + `VGA` |
| Linux aarch64 | `aarch64` | `linux-aarch64` | `kvm` | `virt` + `virtio-gpu-pci` + UEFI |

### 各宿主平台安装与权限要求

* **Windows (`WHPX`)**：安装 `qemu-system-x86_64` 与 `qemu-img`，在主板固件中开启虚拟化，并在 Windows 功能中启用 **Windows Hypervisor Platform**。
* **macOS (`HVF`)**：通过 Homebrew 执行 `brew install qemu` 安装原生构建的 `qemu-system-aarch64` / `qemu-system-x86_64` 与 `qemu-img`。
* **Linux (`KVM`)**：安装发行版对应的 `qemu-system-x86` / `qemu-system-arm` 与 `qemu-utils`，并将当前运行用户加入 `kvm` 用户组以访问 `/dev/kvm`。
* **非特权执行**：所有平台均必须以普通非管理员用户运行 QEMU。

### UEFI 固件自动发现与变量隔离

* **AArch64 固件发现**：`aarch64` Profile（`virt` 机器模型）会自动从常见的 QEMU 安装目录中搜索匹配的 EDK2/AAVMF 固件代码（`code`）与变量存储模板（`vars`）。若自动发现失败，可在 Profile 中显式指定：
  ```toml
  [qemu.firmware]
  code = "/path/to/edk2-aarch64-code.fd"
  vars = "/path/to/edk2-arm-vars.fd"
  ```
* **每实例独立变量存储**：Palette 在启动实例或创建持久虚拟机时，会将 `vars` 模板复制到 `.runtime/` 下该实例/定义的私有目录中；执行 `vm reset` 或桌面端“恢复初始状态”时，也会用干净模板替换该副本，确保基础固件模板永不被污染。
* **x86 UEFI 镜像**：若你的 `x86_64` 客户机镜像采用 UEFI 而非传统 BIOS 安装，请在对应 Profile 中显式配置匹配的 OVMF `code` 与 `vars` 路径。

## 2. 显式跨架构软件模拟（TCG Profiles）

当需要在不同架构的宿主机上运行客户机（例如在 Apple Silicon Mac 上运行 `x86_64` 桌面，或在无硬件虚拟化的环境中调试）时，可显式选择内置的软件模拟 Profile：

* **`tcg-x86_64`**：使用 `qemu-system-x86_64`、`accelerator = "tcg"`、`machine = "q35"` 与 `cpu = "max"`。
* **`tcg-aarch64`**：使用 `qemu-system-aarch64`、`accelerator = "tcg"`、`machine = "virt"` 与 `cpu = "max"`。
* **`tcg-debug`**：保留的 `x86_64` 空白盘软件模拟调试配置。

> **TCG 注意事项**：
> 1. 在 TCG 模式下配置 `cpu = "host"` 会被直接拒绝；内置 TCG Profile 统一使用 `cpu = "max"`。
> 2. 在 Apple Silicon 上使用 `--profile tcg-x86_64` 只需宿主原生 ARM64 构建的 `qemu-system-x86_64`，由 QEMU TCG 引擎动态翻译指令，**无需安装 Rosetta**。
> 3. 软件模拟速度显著慢于硬件加速，因此内置 TCG Profile 放宽了超时预算（启动至首帧就绪 `600s`、QMP 握手 `60s`、单条 QMP 命令与截图 `30s`、单次动作 `60s`、`frame_ttl_seconds = 60s`）。
> 4. **切换 Profile 不会转换磁盘镜像架构**：`x86_64` 镜像只能由 `x86_64` Profile 启动，`aarch64` 镜像只能由 `aarch64` Profile 启动；`qemu-img convert` 只改变容器格式（`raw` ↔ `qcow2`），不改变客户机指令集或引导方式。

在 Apple Silicon 上使用 `tcg-x86_64` 启动 `x86_64` 镜像示例：

```bash
uv run palette doctor --profile tcg-x86_64
uv run palette daemon start
uv run palette vm start --vm-id x86-desktop --profile tcg-x86_64     --image /absolute/path/to/debian-13-xfce-x86_64.qcow2
```

## 3. 进程生命周期监管与内存上限执行

为防止 QEMU 子进程在后台崩溃后残留失控或耗尽宿主机内存，Palette 按宿主操作系统实施双层进程保护：

| 维度 | Windows 实现 (`win32`) | macOS / Linux 实现 (`POSIX`) |
| --- | --- | --- |
| **孤儿进程回收** | 将 QEMU 绑定至开启 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Windows **Job Object**；服务进程退出或被强杀时由内核立即终止 QEMU | 启动独立的 **POSIX Supervisor** 守护进程持有 QEMU 进程组，并监听私有 liveness 管道；服务退出或被 `SIGKILL` 时管道关闭，立即清理整个进程组 |
| **宿主内存上限 (`process_memory_limit_mib`)** | 通过 Windows Job Object 内核硬上限强制执行 | 由 `psutil` 每 **250 ms** 采样 QEMU 及其所有子孙进程的 RSS 总和；一旦超出预算即终止进程组（采样式限制允许极短暂超调） |
| **最长运行时间 (`process_max_runtime_seconds`)** | 由服务与进程监管器共同强制执行 | 由独立 Supervisor 脱离主事件循环计时并强制终止超时进程组 |
| **QMP 传输隔离** | 仅当前用户可访问的 AF_UNIX 套接字，服务重启后不可重新接管旧进程，需清理并创建新实例 | 匿名私有管道直连（Supervisor 不解析、不代理 QMP 流量），服务重启后创建新实例 |

`palette doctor`、`vm_status` 以及每个实例的 `manifest.json` 均会明确记录当前的宿主架构、客户机架构、所选加速器、是否跨架构运行以及内存限制执行方式。
