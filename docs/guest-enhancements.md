# Guest Enhancements

**English** | [简体中文](guest-enhancements.zh-CN.md)

Palette reuses QEMU Guest Agent, SPICE vdagent and spice-webdavd. The desktop remains usable without these guest services: missing responses mean **currently unavailable**, not proof that software is uninstalled. Availability is checked repeatedly, including after guest reboot. Existing `org.palette.tools.0` MCP text/app tools remain a separate protocol; installing QGA does not implement them.

## Install inside a Debian / XFCE guest

Copy `guest/linux/` into the guest and run **inside the VM**:

```sh
sudo sh guest/linux/install.sh
```

The script installs distribution packages `qemu-guest-agent`, `spice-vdagent`, `spice-webdavd`, `x11-xserver-utils`, `python3`, and `gvfs-backends`. It installs an unprivileged X11 session helper and a permission rule limited to its readiness port. Log out and in afterward. Set the VM's graphics device to **virtio-gpu** in its powered-off hardware settings, then restart. VGA remains supported for basic display/input and clipboard, but cannot supply this automatic resolution feature.

To remove Palette's session helper and autostart entry (distribution packages remain installed), run inside the guest:

```sh
sudo sh guest/linux/install.sh --uninstall
```

Log out and in to stop an already running helper. The helper applies changed virtio-gpu preferred modes to connected virtual XRandR outputs and sends a readiness heartbeat only after a successful X11 display query. It does not run as root, accept commands, or expose a guest shell. Wayland and Windows guest automatic resolution are not implemented by this helper. Windows guests may install the upstream [SPICE tools](https://www.spice-space.org/download.html) and [QGA/virtio drivers](https://virtio-win.github.io/Knowledge-Base/Qemu-ga-win.html) for the other applicable capabilities; those require separate guest acceptance.

## Host profile configuration

Normal bundled profiles enable these optional channels; `strict-example` keeps them off. Existing saved VM hardware choices are retained. Configuration defaults for custom profiles are off:

```toml
[guest_agent]
enabled = true
request_timeout_seconds = 3.0
max_file_bytes = 16777216

[guest_desktop]
clipboard = true
resize = true
shared_folders = true
max_clipboard_bytes = 49152

# Optional persistent export, configured by the trusted host operator:
[[shared_folders]]
tag = "documents"
path = "exchange"
read_only = true
```

Relative export paths resolve from the configuration root. Only configured or explicitly selected directories are exposed. QGA and WebDAV use separate per-instance private Unix sockets on POSIX or named pipes on Windows; no guest management/file-sharing TCP port is opened on the host. Restart the shared daemon after upgrading the backend and restart the VM after channel configuration changes. On Windows, host pipe connections start before QMP because QEMU's pipe initialization waits for a client.

## Desktop use and degradation

Open **增强工具** in the VM console. The panel updates automatically every three seconds.

| Feature | Behavior | If unavailable |
| --- | --- | --- |
| Clipboard | Explicit buttons copy text in either direction, or send typed Unicode/CJK text; maximum 48 KiB UTF-8 | Buttons disabled; physical keyboard remains available. After sending, take control and press Ctrl+V in the guest |
| Automatic resolution | Opt in using **自动分辨率**; requests are debounced while human control is held. Matching display changes release held input, synchronize geometry and reacquire control while focused | Existing framebuffer and viewport scaling remain available; an unconfirmed resize disables the option |
| Guest information | Agent version, OS, hostname and interface IPs | QEMU lifecycle/readiness remains visible; guest fields unavailable |
| Time synchronization | Explicit host UTC timestamp sent through an enabled QGA command | Disabled; guest's own timekeeping continues |
| Safe Desktop shutdown | Known-online QGA shutdown, otherwise existing ACPI shutdown | Timeout is reported without automatically forcing power off. A result-unknown QGA operation is not retried through ACPI |
| File transfer | Main-process host file dialogs; guest absolute path; 48 KiB chunks; maximum 16 MiB per file; progress and cancel | Disabled when required QGA commands are absent |
| Shared folders | Read-only by default; selected host directory available via guest WebDAV | Service status unavailable; basic VM use unaffected |

Each explicit enhancement operation acquires human ownership, shares the input scheduler, and releases ownership afterward. Host clipboard content and filesystem dialogs stay in Electron main. Transfers are cancelled on loss of host-window focus. Downloads use a temporary host file and replace the destination only after complete transfer and guest close. Uploads overwrite the selected guest path; cancellation or disconnect may leave a partial guest file. There is no automatic replay of uncertain writes, clipboard operations or shutdown requests.

## Shared folders on Windows, macOS and Linux hosts

The private multiplex protocol is compatible with guest `spice-webdavd`; Palette uses [WsgiDAV](https://github.com/mar10/wsgidav) as the host WebDAV implementation. Full SPICE display transport, SMB and virtiofs are not required. Inside a guest running spice-webdavd, open:

```text
dav://127.0.0.1:9843/
```

Use **选择共享目录** to select a host directory. **只读共享** defaults on; turn it off only when guest writes are intended. A GUI selection replaces the active exports and lasts until the VM stops; trusted profile `[[shared_folders]]` exports are restored on the next start. **停止共享** removes active exports. Export traversal and symlink escape are blocked by the upstream filesystem provider; error responses omit host filesystem diagnostics. Each request/response is limited to 16 MiB and four guest HTTP clients. Protocol support covers the current little-endian x86_64/aarch64 targets. Actual mount behavior still depends on the guest WebDAV client.

## Verification

| Target | Verification status |
| --- | --- |
| Windows host, QEMU 11.1, explicit TCG, blank scratch disk | Real startup, QMP/framebuffer, all private host channels, disabled guest capabilities without installed tools, and process cleanup verified |
| Electron console on Windows | Existing console flow and the new unavailable-enhancement panel/control flow verified; screenshot generated by Playwright |
| Linux/macOS hosts | POSIX channel implementation included; no physical acceptance claimed on this Windows development host |
| Debian/XFCE guest with installed tools | Installer/helper included; clipboard, live resizing, QGA operations and WebDAV mounting still require real desktop-image acceptance |
| Windows/Wayland guests | No automatic-resolution helper; upstream QGA/SPICE/WebDAV capabilities require separate acceptance |

`palette doctor` reports host virtio-serial and qemu-vdagent prerequisites as nonblocking checks. Runtime `vm_status.enhancements` and operator `guest.status` expose actual availability; QGA readiness never implies an interactive desktop is ready.

Unit/integration tests cover delayed service startup, fragmented messages, QGA resynchronization after timeouts, disabled commands, ownership fencing, uncertain shutdown, transfer limits/cleanup, Unicode clipboard and decompression bounds, and read-only/Unicode/traversal WebDAV behavior. Electron tests cover the unavailable-feature panel and continued basic console control.

Real-guest acceptance is separate: install the packages in a real image, verify Chinese/Emoji in both clipboard directions, resize while focused and unfocused, compare transferred file hashes, mount/read/write read-only and writable exports, stop/restart services, reboot and retest availability, and verify timed-out shutdown never force-kills the guest. Mock tests and QEMU firmware-only smoke tests do not establish physical guest acceptance.

Upstream references: [QGA protocol](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html), [SPICE folder sharing](https://www.spice-space.org/spice-user-manual.html#_folder_sharing), [QEMU clipboard implementation](https://github.com/qemu/qemu/blob/master/ui/vnc-clipboard.c).
