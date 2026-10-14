# Guest Base Images Directory (`images/`)

**English** | [简体中文](README.zh-CN.md)

Place your **read-only guest desktop base images** in this directory (for example, `debian-13-xfce-x86_64.qcow2` or `debian-13-xfce-aarch64.qcow2`) and reference them from `profiles/*.toml`.

## 1. Referencing an Image in a Profile

Paths in `[images]` are resolved relative to the configuration root directory (where `palette.toml` lives):

```toml
[images]
base = "images/debian-13-xfce-x86_64.qcow2"
format = "qcow2"
sha256 = "<64-hex-character sha256 digest>"
size_gib = 24
```

* **Separate Images per Guest ISA**: `aarch64` profiles (`macos-aarch64`, `linux-aarch64`, `tcg-aarch64`) require a dedicated `images/debian-13-xfce-aarch64.qcow2` built with AArch64 UEFI boot and VirtIO drivers.
* **Ignored by Git**: Disk images are large and carry OS distribution licensing obligations, so all image files in this directory are ignored by Git. What gets committed is the profile's record of the image — its path, container format, and `sha256` digest — which turns a file path into an immutable, verified boot artifact.

## 2. Verification & Read-Only Immutability

1. **Building a Template**: Follow the [guest template build guide](../docs/guest-template.md) to create and sanitize a base image.
2. **Probing Format & SHA-256**: Verify the image with `palette doctor`:
   ```bash
   uv run palette doctor --hash-base
   ```
   `palette doctor` inspects the true container format via `qemu-img info` (rather than trusting the file extension) and verifies the SHA-256 digest when pinned.
3. **Never Modified at Runtime**: Files in this directory are **never written to or committed into**. Every VM run creates its own writable `qcow2` overlay under `.runtime/`; resetting a VM replaces that overlay while leaving the base image in `images/` byte-for-byte untouched.
