# 平台与客户机架构

[English](platforms.md) | **简体中文**

Palette 默认选择原生架构客户机。`qemu.arch` 描述客户机指令集；QEMU 可执行文件自身应使用宿主
架构的构建。本项目不附带客户机镜像。

## 原生 Profile

`host.default_profile = "auto"` 按下表选择 profile。缺少加速器或宿主/客户机架构不兼容时明确报错，
不会自动切换到 TCG。

| 宿主 | 客户机 | Profile | 加速器 | 机器与显示 |
| --- | --- | --- | --- | --- |
| Windows x86_64 | x86_64 | `default` | WHPX | `q35`、VGA |
| macOS Apple Silicon | aarch64 | `macos-aarch64` | HVF | `virt`、virtio GPU、UEFI |
| macOS Intel | x86_64 | `macos-x86_64` | HVF | `q35`、VGA |
| Linux x86_64 | x86_64 | `linux-x86_64` | KVM | `q35`、VGA |
| Linux aarch64 | aarch64 | `linux-aarch64` | KVM | `virt`、virtio GPU、UEFI |

安装对应的 `qemu-system-x86_64` 或 `qemu-system-aarch64`，以及 `qemu-img`。macOS 可通过 Homebrew
执行 `brew install qemu`。Linux 安装发行版中对应目标架构的 QEMU system 包，并授予服务用户
访问 `/dev/kvm` 的权限。Windows 需要开启固件虚拟化与 Windows Hypervisor Platform。所有平台
都应使用普通用户运行 QEMU。

AArch64 profile 会从常见 QEMU 安装目录发现 EDK2/AAVMF code 与变量存储模板。发现失败时，显式
配置 `qemu.firmware.code` 和 `qemu.firmware.vars`，二者必须是一套匹配客户机架构的固件。
Palette 为每个实例复制变量存储，干净重置也会替换这份副本。采用 UEFI 安装的 x86 镜像需要
显式配置对应的 x86 固件。

## 显式软件模拟

`tcg-x86_64` 与 `tcg-aarch64` 在受支持宿主上调用对应的 system emulator，使用
`accelerator = "tcg"` 和 `cpu = "max"`；TCG 下的 `cpu = "host"` 会被拒绝。`tcg-debug` 保留为
旧的 x86_64 调试 profile。TCG 不要求宿主 ISA 匹配，也不依赖硬件 hypervisor；只有显式选择
时才启用，不会自动回退。

在 Apple Silicon 上运行 x86_64 客户机，先保持 `serve` 运行，再执行：

```bash
uv run palette doctor --profile tcg-x86_64
uv run palette vm start --vm-id x86-desktop --profile tcg-x86_64 \
    --image /absolute/path/debian-x86_64.qcow2
```

使用宿主原生构建的 `qemu-system-x86_64` 即可，由 TCG 翻译客户机指令，无需 Rosetta。性能预计
低于原生客户机。Profile 保持有限预算：实例构建至首帧就绪共 600 秒、QMP 握手 60 秒、QMP 命令和截图 30 秒、
单次动作 60 秒。有效显示缓冲区仍可能只是固件画面，不能证明桌面已经启动完成。

选择加速器不会转换镜像架构或启动方式。客户机 ISA、BIOS/UEFI 模式及存储/显示驱动必须匹配
所选机器。raw 转 qcow2 只改变磁盘容器；qcow2/raw 元数据不能证明客户机 ISA，应在模板说明和
profile 中记录它。详见[客户机模板](guest-template.zh-CN.md)。

## 进程寿命与资源限制

Windows 将 QEMU 加入 kill-on-close Job Object，并设置配置的内存限制。macOS 与 Linux 使用
独立 supervisor 持有 QEMU 进程组、监测私有 liveness pipe。服务正常退出或被强杀都会关闭该
管道并触发进程组清理。QMP 仍通过 Palette 与 QEMU 间的直接私有管道传输，supervisor 不解析
或代理 QMP。匿名 QMP 管道不能由重启后的服务恢复，因此服务故障后要重新启动 `serve` 并创建
新实例。

POSIX 上由 psutil 每 250 ms 采样 QEMU 及其后代进程的 RSS 总量；超过
`resources.process_memory_limit_mib` 后结束进程组。这是允许短暂超调的采样预算，不是与
Windows Job Object 等价的硬上限。supervisor 还会独立于服务事件循环执行
`process_max_runtime_seconds` 时限。`doctor`、状态和 manifest 会报告宿主/客户机 ISA、加速器、
是否跨架构以及内存限制方式。进程组不能约束主动创建新会话的程序；任意 QEMU 参数仍位于 Palette
隔离保证之外。

## 验证状态

自动化测试使用测试进程覆盖 profile 选择、命令构建、私有管道、取消、进程回收和资源限制。
**本次变更没有执行** WHPX、HVF、KVM 或跨架构 TCG 搭配真实桌面镜像的实机验收矩阵。
请在每个目标宿主和镜像上运行[运维探针](operator.zh-CN.md)。E2E 被跳过或 CI 通过，不构成
桌面兼容性或性能结论。
