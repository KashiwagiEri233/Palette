# Platforms, Guest Architectures & Resource Supervision

**English** | [简体中文](platforms.zh-CN.md)

**Palette** defaults to native hardware-accelerated guests matching the host CPU architecture. In profile configurations, `qemu.arch` specifies the **guest instruction set architecture (ISA)**, while the QEMU binary itself should always be a native build for the host OS.

> Palette does not distribute guest OS images. Supply a base image or ISO whose ISA, firmware mode (BIOS vs. UEFI), and storage/display drivers match the target machine (see the [guest template guide](guest-template.md)).

## 1. Native Hardware-Accelerated Profiles

Setting `host.default_profile = "auto"` in `palette.toml` selects the native profile for your host OS and CPU architecture from the matrix below. If the required accelerator is missing or the host/guest ISA is incompatible, Palette fails with an explicit diagnostic and **never falls back to software emulation (`tcg`) automatically**.

| Host Platform | Guest ISA | Default Profile ID | Accelerator | Machine & Default Display |
| --- | --- | --- | --- | --- |
| Windows x86_64 | `x86_64` | `default` | `whpx` | `q35` + `VGA` |
| macOS Apple Silicon | `aarch64` | `macos-aarch64` | `hvf` | `virt` + `virtio-gpu-pci` + UEFI |
| macOS Intel | `x86_64` | `macos-x86_64` | `hvf` | `q35` + `VGA` |
| Linux x86_64 | `x86_64` | `linux-x86_64` | `kvm` | `q35` + `VGA` |
| Linux aarch64 | `aarch64` | `linux-aarch64` | `kvm` | `virt` + `virtio-gpu-pci` + UEFI |

### Host Prerequisites

* **Windows (`WHPX`)**: Install `qemu-system-x86_64` and `qemu-img`, enable virtualization in BIOS/UEFI, and turn on the **Windows Hypervisor Platform** feature.
* **macOS (`HVF`)**: Install QEMU via Homebrew (`brew install qemu`) to get native `qemu-system-aarch64` / `qemu-system-x86_64` and `qemu-img` binaries.
* **Linux (`KVM`)**: Install the distribution's `qemu-system-x86` / `qemu-system-arm` and `qemu-utils` packages, and grant your user account access to `/dev/kvm`.
* **Non-Privileged Execution**: Always run Palette and QEMU as a standard non-admin user.

### UEFI Firmware Discovery & Per-Instance Variable Isolation

* **AArch64 Firmware Auto-Discovery**: `aarch64` profiles (`virt` machine) automatically probe common QEMU installation directories for matching EDK2/AAVMF firmware `code` and variable store (`vars`) templates. If auto-discovery fails, configure them explicitly in the profile:
  ```toml
  [qemu.firmware]
  code = "/path/to/edk2-aarch64-code.fd"
  vars = "/path/to/edk2-arm-vars.fd"
  ```
* **Isolated Variable Stores**: Palette copies the `vars` template into each instance's or persistent VM's private directory under `.runtime/`. Running `vm reset` or **Restore Initial State** in Palette Desktop replaces that copy from the clean template so the base firmware image is never mutated.
* **x86 UEFI Guests**: If an `x86_64` guest image was installed with UEFI rather than legacy BIOS, configure the matching OVMF `code` and `vars` paths in its profile.

## 2. Explicit Cross-ISA Software Emulation (`TCG` Profiles)

To run a guest ISA that differs from the host CPU (for example, running an `x86_64` guest on Apple Silicon) or to run on a host without a hardware hypervisor, explicitly select one of the built-in TCG profiles:

* **`tcg-x86_64`**: Runs `qemu-system-x86_64` with `accelerator = "tcg"`, `machine = "q35"`, and `cpu = "max"`.
* **`tcg-aarch64`**: Runs `qemu-system-aarch64` with `accelerator = "tcg"`, `machine = "virt"`, and `cpu = "max"`.
* **`tcg-debug`**: Legacy `x86_64` scratch-disk software emulation profile.

> **Important TCG Notes**:
> 1. `cpu = "host"` is rejected under TCG; all TCG profiles use `cpu = "max"`.
> 2. On Apple Silicon, `--profile tcg-x86_64` uses a native ARM64 build of `qemu-system-x86_64` to translate guest instructions via TCG — **Rosetta is not required**.
> 3. Because software emulation is roughly an order of magnitude slower than hardware virtualization and offers **no hardware guest-isolation guarantee**, TCG profiles configure extended bounded timeouts (`600s` startup to first frame, `60s` QMP handshake, `30s` QMP command/capture, `60s` per action, and `60s` `frame_ttl_seconds`).
> 4. **Selecting an accelerator does not convert a disk image**: an `x86_64` disk image requires an `x86_64` profile, and an `aarch64` image requires an `aarch64` profile. `qemu-img convert` only changes the container format (`raw` ↔ `qcow2`), not the guest ISA or boot mode.

Example: booting an `x86_64` guest on Apple Silicon using `tcg-x86_64`:

```bash
uv run palette doctor --profile tcg-x86_64
uv run palette daemon start
uv run palette vm start --vm-id x86-desktop --profile tcg-x86_64     --image /absolute/path/to/debian-13-xfce-x86_64.qcow2
```

## 3. Process Supervision & Host Memory Caps

Palette enforces process lifetime and host memory ceilings using platform-native mechanisms so an orphaned or runaway QEMU process cannot outlive its owner:

| Mechanism | Windows (`win32`) | macOS / Linux (`POSIX`) |
| --- | --- | --- |
| **Orphan Cleanup** | Places QEMU inside a Kill-on-Close **Windows Job Object** (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`); OS kernel terminates QEMU immediately if the owner exits or crashes | Spawns a dedicated **POSIX Supervisor** holding the QEMU process group and watching a private liveness pipe; closing or killing the owner closes the pipe and reaps the process group |
| **Memory Cap (`process_memory_limit_mib`)** | Hard kernel limit enforced by the Windows Job Object | Sampled via `psutil` every **250 ms** across QEMU and all descendant processes; kills the process group when total RSS exceeds the budget (permits brief transient overshoot) |
| **Max Runtime (`process_max_runtime_seconds`)** | Enforced by the service and process guard | Enforced independently by the POSIX supervisor even if the main event loop is blocked |
| **QMP Pipe Lifecycle** | Private anonymous pipes; if the service dies, the Job Object reaps QEMU and a restarted service creates a fresh instance | Private anonymous pipes direct between Palette and QEMU (the supervisor never parses or proxies QMP) |

`palette doctor`, `vm_status`, and each instance's `manifest.json` report the active host ISA, guest ISA, accelerator, cross-architecture status, and memory enforcement mode.
