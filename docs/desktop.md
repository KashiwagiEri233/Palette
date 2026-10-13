# Desktop application

**English** | [简体中文](desktop.zh-CN.md)

Palette Desktop manages local QEMU machines and shares a runtime with MCP clients.
The interface supports importing qcow2/raw images, installing from ISO, persistent
disks, a live console, and exclusive human keyboard/mouse control.

## Run from source

Install Node.js 22.12 or newer, uv, Python 3.12/3.13, and QEMU for the desired guest
architecture. Run these commands from the Palette checkout:

```bash
uv sync --locked
npm --prefix desktop ci
npm --prefix desktop run dev
```

For a production build and local preview:

```bash
npm --prefix desktop run build
npm --prefix desktop run preview
```

The first release does not bundle Python or QEMU. The launcher runs the Python module
from this checkout with an explicit uv project, independently of the configuration
directory selected in Settings. Set `PALETTE_UV` to the absolute uv executable if it
is not on PATH. `PALETTE_ROOT` selects an initial configuration directory.

On Windows, the title bar is integrated into the workspace with native minimize,
maximize, and close buttons. Drag an empty area of the top bar or the Palette brand
to move the window; Windows window snapping remains available. The default menu bar
is hidden. The minimum window size is 960 × 640 logical pixels; system display scaling
is respected. Chinese text uses Microsoft YaHei, and long forms scroll within the
window. In machine settings, the heading and save/cancel actions stay visible while
the settings body scrolls.
When a dialog opens, the native window button area darkens with its backdrop and
returns to its normal color after the last dialog closes.

## Create and manage machines

Choose an image or ISO, a matching architecture/profile, CPU, memory, disk capacity,
and network mode. Imported images remain read-only backing files: keep them and their
backing chains at the original locations. A disk capacity smaller than the source
image is rejected. Increasing the virtual capacity does not resize guest partitions.
The selected firmware and virtual disk drivers must match the guest; Windows ISO
installations may require VirtIO drivers supplied on installation media.

VM definitions are stored under `<runtime>/definitions`. Their disk and UEFI variable
stores survive shutdown and new process instances. **虚拟机设置** is available from each
machine's action menu and detail header. Existing CLI-created machines remain temporary desktops;
CLI lifecycle commands honor existing Desktop definitions and their persistent disks.

Restart preserves the disk. **Restore initial state** discards changes: an imported
image returns to its original base; an ISO installation returns to an empty disk.
Deletion removes managed files subject to dependency checks, never the imported base.
Normal GUI shutdown reports an ACPI timeout and leaves the machine running. Forced
stop is a separate confirmed operation. Closing the window asks whether to keep
machines running, shut them down, or cancel.

## Virtual machine settings

The settings panel groups General, CPU and memory, Disk and boot, Display, Network, and
Sound. Running machines are read-only; shut down before changing hardware. Save applies
changes to the next start, with revision checks preventing stale edits. Unsaved changes
can be saved, discarded, or kept for further editing.

CPU supports 1–64 vCPUs; RAM supports 256–1048576 MiB. Host resource information is a
reference, not a reservation. Disk expansion is a separate confirmed operation and requires
saving or discarding other edits first. Capacity can only grow, up to 4096 GiB. The panel
distinguishes configured capacity, actual virtual capacity, and host allocation. Expansion
does not enlarge guest partitions. Only managed writable layers change; backing images and
UEFI variables remain intact. Interrupted expansion is recovered from the disk and journal;
uncertain integrity blocks startup rather than attempting a destructive shrink.

Choose, replace, mount, or eject an ISO for either source type. Boot order offers automatic,
disk-first, and CD-first; CD-first requires mounted media. Automatic selects media on the
first start and disk afterwards. Existing UEFI boot entries may affect the guest's choice.

Display and NIC options are filtered by architecture and installed QEMU capabilities.
Supported display devices can request a default resolution, while RAMFB delegates it to
firmware/the guest. This is a startup hint, not guaranteed guest resolution or console zoom.
Network offers offline, restricted, and NAT modes, plus NIC model, MAC address and TCP/UDP
forwarding bound to `127.0.0.1`. Existing trusted profile network options remain visible and
preserved; conflicting hardware overrides are rejected.

Sound uses Intel HDA output to the host default audio device, without microphone capture.
QEMU must offer CoreAudio on macOS, DirectSound on Windows, or PipeWire/PulseAudio/ALSA on
Linux; the guest also needs a sound driver. An unavailable backend is reported explicitly.
The creation wizard reuses the hardware forms under **更多硬件选项**. Version-1 definitions
are read without rewriting storage and upgraded on a successful save. An older owner shows
an upgrade notice; Desktop never restarts that owner automatically.

## Console and control

The console initially shows the desktop without sending input. Choose **Take control**
to acquire a per-machine lease, then click the display to focus it. Held keys, mouse
dragging, clicks and scrolling use the same controller as Agent actions. Physical keys
use the guest keyboard layout; direct host IME/clipboard transfer is not provided.
The toolbar sends Ctrl+Alt+Delete and Super through that controller.

Ctrl+Alt+G releases control. Leaving the console, losing focus, hiding the window,
changing guest resolution or disconnecting also releases input. A missed heartbeat
expires the lease after three seconds. Agent observations continue during human
control; Agent writes are refused. Input with an uncertain result is never replayed.

The display gateway runs only on loopback, uses single-use display tickets, and
filters the RFB protocol. noVNC cannot send input even if its view-only flag is changed.
QEMU credentials remain in the backend; operator credentials remain in Electron main.
The existing Agent screenshots still use QMP and retain their original frame fences.

## Shared backend and MCP

```bash
uv run palette daemon start
uv run palette daemon status
uv run palette serve --connect-only
uv run palette daemon stop
```

`daemon run` runs in the foreground. Desktop connects to an existing owner or starts a
daemon. `serve` connects to an existing compatible owner; without one it retains the
original stdio-owned lifetime. `serve --standalone` explicitly requires its own owner.
A proxy disconnect does not stop the daemon. When Desktop attaches to a stdio-owned
service, it identifies that mode: the MCP session ending still stops those machines.
Stopping a daemon closes all its managed processes; use GUI shutdown first when guest
data needs a graceful shutdown. After updating backend source, restart the owner.

## Verification

The “状态” tab polls lifecycle, instance, control owner, action sequence, queued tasks and
held inputs every two seconds. Follow the [acceptance procedure](acceptance-test.md) to
test MCP connection, actual clicks and GUI synchronization.

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python scripts/check_docs.py
npm --prefix desktop test
npm --prefix desktop run test:e2e
```

Electron integration tests use disposable configuration directories and supervised
fake QEMU processes, including a real local RFB connection. They test transport and
UI behavior, not guest compatibility, hardware acceleration or real desktop latency.
Physical acceptance still requires a real QEMU and matching guest image, as described
in [the platform guide](platforms.md) and [operator guide](operator.md).
