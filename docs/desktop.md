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

## Create and manage machines

Choose an image or ISO, a matching architecture/profile, CPU, memory, disk capacity,
and network mode. Imported images remain read-only backing files: keep them and their
backing chains at the original locations. A disk capacity smaller than the source
image is rejected. Increasing the virtual capacity does not resize guest partitions.
The selected firmware and virtual disk drivers must match the guest; Windows ISO
installations may require VirtIO drivers supplied on installation media.

VM definitions are stored under `<runtime>/definitions`. Their disk and UEFI variable
stores survive shutdown and new process instances. Stopped definitions can be renamed
and their CPU, memory, network and ISO attachment changed. Installation media can be
removed after installation. Existing CLI-created machines remain temporary desktops;
CLI lifecycle commands honor existing Desktop definitions and their persistent disks.

Restart preserves the disk. **Restore initial state** discards changes: an imported
image returns to its original base; an ISO installation returns to an empty disk.
Deletion removes managed files subject to dependency checks, never the imported base.
Normal GUI shutdown reports an ACPI timeout and leaves the machine running. Forced
stop is a separate confirmed operation. Closing the window asks whether to keep
machines running, shut them down, or cancel.

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
