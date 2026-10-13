# Guest template

**English** | [简体中文](guest-template.zh-CN.md)

How to build the read-only desktop image that `profiles/*.toml` points at. This build
ships no image: an image is a large, licensed, host-specific artefact, and the plan
requires the operator to record where it came from and what is in it.

## What the template must be

| Property | Value | Why it is fixed |
| --- | --- | --- |
| Distribution | Debian 13 (trixie), x86_64 or aarch64 matching `qemu.arch` | The original P0 uses x86_64; ARM targets need a separate image |
| Desktop | XFCE on X11 | Predictable keyboard/pointer handling; Wayland compositors vary |
| Resolution | 1280×800 at 100% scale | Configured *inside* the guest, not by a kernel argument |
| Keyboard | US layout | Strict text mode assumes US; a different layout silently mistypes |
| User | ordinary, non-root, auto-login only for a controlled development template | The plan forbids host accounts and production credentials in a shareable template |
| Network | none needed for the fixture | The template should work with the `offline` profile network mode |
| Fixture | `guest/fixtures/offline-workbench.html` | Gives clicks, drags, scrolls and typing a visible result |

## Build sketch

Choose the guest ISA and BIOS/UEFI boot mode before installing. `virt`/aarch64 uses matching
AArch64 UEFI and virtio display/storage drivers; x86 `q35` images must match the configured
BIOS or x86 UEFI. Switching to TCG does not convert the installed system. The complete
host/guest combinations and firmware setup are in [platforms](platforms.md).

The example below builds an x86_64 image from the configuration root. For AArch64, install
the matching guest and use `images/debian-13-xfce-aarch64.qcow2` instead; renaming or converting
an x86_64 disk does not change its architecture.

1. Install from trusted media, recording the exact version and the media's SHA-256.
2. Install the desktop, a browser, a text editor and the fixture above (for example at
   `~/palette-fixture/index.html`).
3. Pin the display to 1280×800, scale 100%, US layout. Verify with `xrandr` inside the
   guest, not from the host.
4. Turn off anything that changes the screen on its own: screen blanking, lock screen,
   notification popups, first-run tours, automatic updates.
5. Work as an ordinary user. Do **not** place host credentials, cookies, SSH keys or real
   documents in the template.
6. Update the system during the build, then stop automatic upgrades: a mid-task package
   upgrade makes the desktop change underneath the agent.
7. Shut down cleanly so the image has a consistent filesystem, then convert and hash it:

   ```sh
   qemu-img convert -O qcow2 template.raw images/debian-13-xfce-x86_64.qcow2
   qemu-img info images/debian-13-xfce-x86_64.qcow2
   sha256sum images/debian-13-xfce-x86_64.qcow2
   ```

8. Put the digest into the profile:

   ```toml
   [images]
   base = "images/debian-13-xfce-x86_64.qcow2"
   format = "qcow2"
   sha256 = "<the digest printed above>"
   ```

Relative paths resolve from the configuration root. A pinned digest is what turns "whatever
file is at that path" into "the image this profile is allowed to run". Without it,
`palette doctor` reports the image as unverified. It checks the actual format using
`qemu-img info`, independently of the filename extension.

## Verifying the template

```bash
uv run palette doctor --json                      # selected native profile
```

Start `uv run palette serve` (or let the MCP client start it) and keep it running. In another
terminal with the same configuration root:

```bash
uv run palette vm start --vm-id desktop
uv run palette vm status --vm-id desktop
```

`desktop_ready` is deliberately reported as `null`: a valid framebuffer may be firmware or
a login prompt, and the service will not guess. Confirm the desktop yourself in the first
screenshot.

## Automated acceptance

Set `PALETTE_E2E=1` and `PALETTE_E2E_PROFILE` to the matching configured profile. Optional
QEMU/image/accelerator overrides are described in the [operator guide](operator.md). Then run:

```bash
uv run pytest tests/e2e -v
```

That suite is the plan's physical gate: 20 start/stop cycles, base-image digest unchanged,
a click that reaches the guest, and a stale frame refused on real hardware. It is skipped
by default, and a skipped test is not evidence of anything.

The native and cross-architecture platform matrix has not undergone physical acceptance in
this change. Record guest ISA, accelerator, firmware and image digest with each real result.

## The guest enhancement service

`guest/agent/` is where an in-guest tool service would live. Nothing is implemented there
yet, on purpose: the plan keeps the enhancement path as a defined contract with a disabled
backend, so enabling it later is a transport implementation rather than an architecture
change. See `guest/agent/README.md` for the contract the service must satisfy.
