# Guest template

**English** | [简体中文](guest-template.zh-CN.md)

How to build the read-only desktop image that `profiles/*.toml` points at. This build
ships no image: an image is a large, licensed, host-specific artefact, and the plan
requires the operator to record where it came from and what is in it.

## What the template must be

| Property | Value | Why it is fixed |
| --- | --- | --- |
| Distribution | Debian 13 (trixie), x86_64 | The plan's P0 baseline |
| Desktop | XFCE on X11 | Predictable keyboard/pointer handling; Wayland compositors vary |
| Resolution | 1280×800 at 100% scale | Configured *inside* the guest, not by a kernel argument |
| Keyboard | US layout | Strict text mode assumes US; a different layout silently mistypes |
| User | ordinary, non-root, auto-login only for a controlled development template | The plan forbids host accounts and production credentials in a shareable template |
| Network | none needed | The profile supplies no NIC; the template must not require one |
| Fixture | `guest/fixtures/offline-workbench.html` | Gives clicks, drags, scrolls and typing a visible result |

## Build sketch

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
   qemu-img convert -O qcow2 template.raw debian-13-xfce-desktop.qcow2
   qemu-img info debian-13-xfce-desktop.qcow2
   sha256sum debian-13-xfce-desktop.qcow2
   ```

8. Put the digest into the profile:

   ```toml
   [images]
   base = "G:/Workspace/Palette/images/debian-13-xfce-desktop.qcow2"
   format = "qcow2"
   sha256 = "<the digest printed above>"
   ```

A pinned digest is what turns "whatever file is at that path" into "the image this profile
is allowed to run". Without it, `palette doctor` reports the image as unverified.

## Verifying the template

```bash
uv run palette doctor --profile default --json     # every check should pass
uv run palette vm start --vm-id desktop
uv run palette vm status --vm-id desktop
```

`desktop_ready` is deliberately reported as `null`: a valid framebuffer may be firmware or
a login prompt, and the service will not guess. Confirm the desktop yourself in the first
screenshot.

## Automated acceptance

With `PALETTE_E2E=1`, `PALETTE_E2E_QEMU` and `PALETTE_E2E_IMAGE` set:

```bash
uv run pytest tests/e2e -v
```

That suite is the plan's physical gate: 20 start/stop cycles, base-image digest unchanged,
a click that reaches the guest, and a stale frame refused on real hardware. It is skipped
by default, and a skipped test is not evidence of anything.

## The guest enhancement service

`guest/agent/` is where an in-guest tool service would live. Nothing is implemented there
yet, on purpose: the plan keeps the enhancement path as a defined contract with a disabled
backend, so enabling it later is a transport implementation rather than an architecture
change. See `guest/agent/README.md` for the contract the service must satisfy.
