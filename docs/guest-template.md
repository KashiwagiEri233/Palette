# Guest Template Build Guide

**English** | [简体中文](guest-template.zh-CN.md)

This guide explains how to build and pin the **read-only desktop base image** referenced by `profiles/*.toml`. Palette does not distribute guest OS images: disk images are large, carry OS distribution licensing obligations, and depend on the target ISA and boot firmware. Operators build or import their own template and pin its SHA-256 digest in the profile.

## 1. Template Baseline Requirements

| Attribute | Target Value | Why It Is Fixed |
| --- | --- | --- |
| **Distribution** | Debian 13 (`trixie`), `x86_64` or `aarch64` matching `qemu.arch` | Keeps environments reproducible; `x86_64` and `aarch64` require separate native installs |
| **Desktop Session** | **XFCE** on X11 | Predictable focus and pointer behavior; Wayland compositors vary widely across virtual GPUs |
| **Resolution & Scale** | `1280 × 800` at `100%` scale | Configured **inside** the guest desktop session to match `[display]` in the profile |
| **Keyboard Layout** | Standard **US** layout | Visual `type_text` emits US hardware key scancodes; another layout will silently mistype punctuation |
| **User Account** | Normal non-`root` user (auto-login only in controlled dev templates) | Shared or reusable templates must never contain host credentials, SSH keys, or production cookies |
| **Network Dependency** | Self-contained offline (`offline` / `restricted-lab` ready) | The desktop and acceptance fixture must boot and function without internet access |
| **Acceptance Fixture** | `guest/fixtures/offline-workbench.html` pre-installed | Gives clicks, double-clicks, drags, wheel scrolls, and keystrokes visible, deterministic feedback |

## 2. Step-by-Step Build Process

Before installing, confirm your guest ISA and firmware mode in the [platform guide](platforms.md):

* **`aarch64` (`virt` machine)**: Use matching AArch64 UEFI (EDK2/AAVMF) firmware and `virtio-gpu-pci` / `virtio-blk-pci` drivers, saving to `images/debian-13-xfce-aarch64.qcow2`.
* **`x86_64` (`q35` machine)**: Match your configured BIOS or x86 OVMF UEFI and `VGA` / `virtio-blk-pci` drivers, saving to `images/debian-13-xfce-x86_64.qcow2`.

> **Note**: Selecting `tcg-x86_64` or `tcg-aarch64` does not convert an installed guest OS, and `qemu-img convert` only changes the disk container format.

1. **Install from Verified Media**: Record the exact OS version and SHA-256 digest of the installer ISO.
2. **Install Desktop & Acceptance Fixture**: Install XFCE (X11), a web browser, a text editor (e.g., `mousepad`), and a file manager (e.g., `thunar`). Copy `guest/fixtures/offline-workbench.html` into the guest user's home directory (e.g., `~/palette-fixture/index.html`).
3. **Lock Display & Keyboard**: Configure `1280 × 800` resolution, `100%` scaling, and the `us` keyboard layout inside the guest session. Verify with `xrandr` inside the guest.
4. **Disable Autonomous Screen Changes**: Turn off screen blanking, lock screens, notification pop-ups, first-run welcome dialogs, and background unattended upgrades so the desktop never changes on its own mid-task.
5. **Sanitize the Image**: Operate as a standard user and ensure no host credentials, cookies, SSH keys, or sensitive documents exist on the disk.
6. **Shut Down Cleanly & Convert to Read-Only `qcow2`**: Shut the guest down gracefully so the filesystem is clean, then convert and hash the image on the host:

```bash
qemu-img convert -O qcow2 template.raw images/debian-13-xfce-x86_64.qcow2
qemu-img info images/debian-13-xfce-x86_64.qcow2
sha256sum images/debian-13-xfce-x86_64.qcow2
```

7. **Pin the Digest in the Profile**: Add the relative path (resolved from the config root) and 64-character hex SHA-256 digest to `profiles/*.toml`:

```toml
[images]
base = "images/debian-13-xfce-x86_64.qcow2"
format = "qcow2"
sha256 = "<64-hex-character sha256 digest>"
size_gib = 24
```

Pinning `sha256` turns "whatever file happens to sit at that path" into "the exact immutable image this profile is authorized to boot." `palette doctor` inspects the actual image container via `qemu-img info` rather than trusting file extensions.

## 3. Verifying the Template

### Step 1: Doctor Probe & Boot Check

```bash
uv run palette doctor --hash-base
uv run palette daemon start
uv run palette vm start --vm-id desktop
uv run palette vm status --vm-id desktop
```

When `screen_ready` reports `true`, QEMU has produced a valid framebuffer. `desktop_ready` intentionally remains `null` unless an in-guest enhancement service completes its handshake. Open the **Palette Desktop Console** or call `vm_observe` to visually confirm the XFCE desktop has booted.

### Step 2: Automated Hardware E2E Suite

With a real QEMU binary and template image configured, run the hardware E2E test suite:

```bash
PALETTE_E2E=1 PALETTE_E2E_PROFILE=default uv run pytest tests/e2e -v
```

`tests/e2e` verifies repeated start/stop cycles, byte-for-byte immutability of the base image, real clicks landing in the guest, and stale-frame rejection on real hardware.

## 4. Optional: In-Guest Enhancement Service

To enable `vm_guest_input_text` (native Unicode/CJK text injection) and `vm_guest_launch_app` (launching registered allowlisted apps), install a guest daemon inside the interactive desktop session speaking the `org.palette.tools.0` (`virtio-serial`) protocol. See the [in-guest agent contract](../guest/agent/README.md) for the specification.

## Optional Guest Enhancements

See the [guest enhancement guide](guest-enhancements.md) for guest installation, automatic capability detection and degradation, Unicode clipboard, automatic resolution, QGA management, file transfer, and private WebDAV shared directories. Missing tools preserve the basic VM console.
