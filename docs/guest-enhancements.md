# Guest Enhancements

**English** | [简体中文](guest-enhancements.zh-CN.md)

Palette reuses QEMU Guest Agent, SPICE vdagent and spice-webdavd. The desktop remains usable without these guest services: missing responses mean **currently unavailable**, not proof that software is uninstalled. Availability is checked repeatedly, including after guest reboot. Unicode clipboard input uses the standard QEMU/SPICE channel and file transfers use QGA. Native text APIs and app launching still require the separate `org.palette.tools.0` custom protocol.

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

## Direct agent operations and degradation

The built-in pi Agent and external MCP clients call enhancements directly, without an operator button, human takeover or file picker.

| Built-in Agent | MCP | Behavior |
| --- | --- | --- |
| `input_text` | `vm_guest_input_text` | Set guest clipboard and send Ctrl+V atomically; Unicode, Chinese, multiline text and Emoji; returns screenshot |
| `clipboard_get` / `clipboard_set` | `vm_guest_clipboard_get` / `vm_guest_clipboard_set` | Read/set guest text clipboard, up to 48 KiB UTF-8; no host clipboard access |
| `file_open` / `file_read` / `file_write` / `file_close` | `vm_guest_file_open` / `vm_guest_file_read` / `vm_guest_file_write` / `vm_guest_file_close` | Upload/download binary contents in QGA chunks without host dialogs |
| `display_resize` | `vm_guest_resize` | Request guest dimensions, then observe actual framebuffer size |
| `sync_time` | `vm_guest_sync_time` | Set guest time to host UTC |
| `status` | `vm_status` | Discover live enhancements, QGA metadata and readiness |

Read `instance_id`, `boot_epoch` and `enhancements` from status first. Text input also needs the latest `frame_id`. Example external MCP input parameters (replace identity/frame values with current results):

```json
{"vm_id":"desktop","instance_id":"current-instance","boot_epoch":1,"frame_id":"latest-frame","request_id":"paste-1","text":"中文输入 😀","method":"clipboard_paste"}
```

Clipboard setting and Ctrl+V share one queued transaction, preventing another writer from intervening. Submission does not prove application response: inspect the returned screenshot. Terminals may need clipboard_set followed by an explicit Ctrl+Shift+V. `native_text` still requires the separate custom service. Unavailable enhancements return `GUEST_TOOL_UNAVAILABLE`; baseline observation and ASCII keyboard input remain usable. Unicode is never silently converted to keyboard events, and services that recover become callable again.

For uploads, open an absolute guest path with `mode="w"` (overwrites the guest file), take `data.transfer_id`, and send base64 chunks through file_write. Always file_close after completion or failure. For downloads, use `mode="r"`, decode each `data.data_base64` until `data.eof=true`, and close. Chunks are limited to 49152 bytes, files to 16 MiB by default, and simultaneous handles to four. Empty uploads need only open/close. Failed uploads may leave partial guest files. QGA accesses guest files with its service privileges; these tools grant no host filesystem access.

Use a fresh request_id for each operation (the built-in Agent generates it). Identical retries return the retained receipt, preventing duplicate paste/write or skipped read chunks; changed parameters with the same ID are rejected. Unknown operations are never automatically replayed; the built-in Agent stops for inspection. All calls share the input scheduler and check operator ownership and Agent run claims. Operator takeover blocks agent writes; handover/run completion closes file handles, and reboot invalidates old boot epochs. The console automatic-resolution checkbox remains optional for human viewing; agents call the resize tool directly.

Desktop graceful shutdown uses known-online QGA or otherwise ACPI. Timeout never automatically forces power off, and an unknown QGA shutdown is not repeated through ACPI.

## Shared folders on Windows, macOS and Linux hosts

The private multiplex protocol is compatible with guest `spice-webdavd`; Palette uses [WsgiDAV](https://github.com/mar10/wsgidav) as the host WebDAV implementation. Full SPICE display transport, SMB and virtiofs are not required. Inside a guest running spice-webdavd, open:

```text
dav://127.0.0.1:9843/
```

Trusted Profile `[[shared_folders]]` entries explicitly configure host exports, read-only by default. Agents access configured exports through guest WebDAV and cannot select arbitrary host directories. The upstream provider blocks traversal and symlink escape, and errors omit host filesystem diagnostics. Requests/responses are limited to 16 MiB with four concurrent guest HTTP clients. Protocol support covers current little-endian x86_64/aarch64 targets; actual mounting still needs guest-specific acceptance.

## Verification

| Target | Verification status |
| --- | --- |
| Windows host, QEMU 11.1, explicit TCG, blank scratch disk | Real startup, QMP/framebuffer, all private host channels, disabled guest capabilities without installed tools, and process cleanup verified |
| Electron console on Windows | Console flow and baseline control with the manual enhancement entry removed are tested; built-in pi SDK model-tool calls have separate contract tests |
| Linux/macOS hosts | POSIX channel implementation included; no physical acceptance claimed on this Windows development host |
| Debian/XFCE guest with installed tools | Installer/helper included; clipboard, live resizing, QGA operations and WebDAV mounting still require real desktop-image acceptance |
| Windows/Wayland guests | No automatic-resolution helper; upstream QGA/SPICE/WebDAV capabilities require separate acceptance |

`palette doctor` reports host virtio-serial and qemu-vdagent prerequisites as nonblocking checks. Runtime `vm_status.enhancements` and operator `guest.status` expose actual availability; QGA readiness never implies an interactive desktop is ready.

Unit/integration tests cover delayed service startup, fragmented messages, QGA resynchronization after timeouts, disabled commands, ownership fencing, uncertain shutdown, transfer limits/cleanup, Unicode clipboard and decompression bounds, and read-only/Unicode/traversal WebDAV behavior. Agent tests cover Unicode input, clipboard, binary transfer without human leases, authenticated IPC, deduplication and takeover. Built-in pi SDK tests drive model tool calls; Electron tests cover continued baseline console control.

Real-guest acceptance is separate: install the packages in a real image, verify Chinese/Emoji in both clipboard directions, resize while focused and unfocused, compare transferred file hashes, mount/read/write read-only and writable exports, stop/restart services, reboot and retest availability, and verify timed-out shutdown never force-kills the guest. Mock tests and QEMU firmware-only smoke tests do not establish physical guest acceptance.

Upstream references: [QGA protocol](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html), [SPICE folder sharing](https://www.spice-space.org/spice-user-manual.html#_folder_sharing), [QEMU clipboard implementation](https://github.com/qemu/qemu/blob/master/ui/vnc-clipboard.c).
