# Platforms and guest architectures

**English** | [简体中文](platforms.zh-CN.md)

Palette chooses a native guest by default. `qemu.arch` describes the guest instruction set;
the QEMU executable itself should be built for the host. No image is included.

## Native profiles

`host.default_profile = "auto"` resolves to the following profile. A missing accelerator or
an incompatible host/guest architecture is an error, never an automatic switch to TCG.

| Host | Guest | Profile | Accelerator | Machine/display |
| --- | --- | --- | --- | --- |
| Windows x86_64 | x86_64 | `default` | WHPX | `q35`, VGA |
| macOS Apple Silicon | aarch64 | `macos-aarch64` | HVF | `virt`, virtio GPU, UEFI |
| macOS Intel | x86_64 | `macos-x86_64` | HVF | `q35`, VGA |
| Linux x86_64 | x86_64 | `linux-x86_64` | KVM | `q35`, VGA |
| Linux aarch64 | aarch64 | `linux-aarch64` | KVM | `virt`, virtio GPU, UEFI |

Install the matching `qemu-system-x86_64` or `qemu-system-aarch64` and `qemu-img`. macOS
Homebrew provides them through `brew install qemu`. On Linux, install your distribution's
QEMU system package for the target architecture and give the service user access to
`/dev/kvm`. Windows needs firmware virtualization and Windows Hypervisor Platform enabled.
Run QEMU as an ordinary user on every platform.

AArch64 profiles discover an EDK2/AAVMF code image and variable-store template in common
QEMU package locations. If discovery fails, configure both `qemu.firmware.code` and
`qemu.firmware.vars`. Use a matching pair for the guest architecture. Palette copies the
variable store into each instance; a clean reset also replaces that copy. An x86 image
installed in UEFI mode needs the corresponding x86 firmware explicitly configured.

## Explicit software emulation

`tcg-x86_64` and `tcg-aarch64` work with the respective system emulator on the supported
hosts. They select `accelerator = "tcg"` and `cpu = "max"`; `cpu = "host"` is rejected for
TCG. `tcg-debug` remains the older x86_64 debugging profile. TCG does not require a matching
host ISA or a hardware hypervisor, and no fallback happens without selecting it.

For an x86_64 guest on Apple Silicon, with `serve` already running:

```bash
uv run palette doctor --profile tcg-x86_64
uv run palette vm start --vm-id x86-desktop --profile tcg-x86_64 \
    --image /absolute/path/debian-x86_64.qcow2
```

Use a host-native `qemu-system-x86_64` executable. TCG translates the guest instructions;
Rosetta is not required. Expect lower performance than a native guest. These profiles use
finite budgets: 600 seconds for provisioning through the first frame, 60 for the QMP handshake, 30 for
QMP commands/capture, and 60 for an action. A framebuffer may still be firmware; it does
not prove that the desktop has finished booting.

Selecting an accelerator does not convert an image's architecture or boot setup. The guest
ISA, BIOS/UEFI mode and storage/display drivers must match the selected machine. Converting
raw to qcow2 only changes the disk container. Qcow2/raw metadata does not prove the guest ISA;
record that in the template description and profile. See [guest templates](guest-template.md).

## Lifetime and resource limits

Windows attaches QEMU to a kill-on-close Job Object, with the configured memory limit.
macOS and Linux use a separate supervisor that owns QEMU's process group and observes a
private liveness pipe. Closing the service, including killing it, closes that pipe and
causes group cleanup. QMP remains a direct private pipe between Palette and QEMU; the
supervisor does not parse or proxy it. Anonymous QMP pipes cannot be recovered by restarting
the service, so restart `serve` and create a new instance after service failure.

On POSIX, psutil samples aggregate QEMU/descendant RSS every 250 ms and kills the group when
`resources.process_memory_limit_mib` is exceeded. This is a sampled budget with possible
transient overshoot, not a Windows Job Object equivalent hard limit. The supervisor also
enforces `process_max_runtime_seconds` independently of the service event loop. `doctor`,
status and the manifest report the host/guest ISA, accelerator, cross-architecture mode and
memory enforcement method. Process groups do not contain a program that deliberately starts
a new session; unrestricted QEMU arguments remain outside Palette's isolation guarantees.

## Validation status

Automated tests cover profile selection, command construction, private pipes, cancellation,
process cleanup and limits using test processes. **This change has not run the physical
acceptance matrix** for WHPX, HVF, KVM or cross-architecture TCG with desktop images. Run the
[operator probe](operator.md) on each intended host and image. A skipped E2E test or a green
CI job is not a desktop compatibility or performance result.
