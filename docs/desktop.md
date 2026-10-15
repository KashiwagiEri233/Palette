# Palette Desktop Guide

**English** | [简体中文](desktop.zh-CN.md)

**Palette Desktop** is the local graphical workbench for Palette (built with Electron, React 19, TypeScript, Tailwind CSS v4, and noVNC). It manages persistent local QEMU virtual machines and shares a single backend runtime (`RuntimeOwner`) with MCP visual agent clients and the Operator CLI.

With Palette Desktop, you can:

* Create persistent VMs by importing read-only `qcow2`/`raw` base images or booting from installer ISOs;
* Configure vCPUs, memory, disk expansion, boot order, display resolution, network/port forwarding, and audio output offline;
* View the live guest screen continuously over a localhost read-only RFB display gateway;
* Click **Take Over** at any time for an exclusive human keyboard/pointer control lease, or reset a VM to its initial state in one click.

## 1. Prerequisites & Running from Source

### Layout and conversations

The workbench has a global rail, VM/conversation navigation, chat, and VM workspace. Home selects a target VM and saves task drafts. VM management retains the creation wizard, console, status, and hardware settings. Settings includes runtime, Agent, permissions, appearance, shortcuts, and About.

Each conversation permanently belongs to one VM; selecting it changes the displayed VM. Create, rename, pin, archive, delete, export, and search are supported. Drafts persist in local application data, isolated by configuration directory. Failed saves retain the current draft with a retry action. Search includes archived conversations.

This delivery implements layout, conversation drafts, and human control. **Built-in Agent execution, credential entry, automatic approvals, and step replay are not connected yet.** Home's save action creates a draft conversation; chat send remains disabled. No simulated replies or model screenshot uploads occur. External MCP agents continue using the existing tool surface.

Minimum window size is1100×700. Below1280px navigation becomes a VM icon strip; below1100 effective CSS pixels the workspace is a drawer. Drag separators or double-click to reset. Layout and light/dark/system preferences persist locally. Ctrl+N creates a conversation, Ctrl+K opens commands, Ctrl+B toggles navigation, and Ctrl+J toggles workspace; macOS uses Cmd equivalents. Ctrl+Alt releases human control, with Ctrl+Alt+G retained; the UI action is “结束控制”.

See the [GUI and built-in Agent specification](desktop-agent-design.md) for subsequent targets; its proposed Agent capabilities are not shipped functionality.

To run Palette Desktop from source, install:

* **Node.js**: `22.12` or newer
* **Python & Package Manager**: Python `3.12` or `3.13` and [uv](https://docs.astral.sh/uv/)
* **QEMU**: `qemu-system-x86_64` or `qemu-system-aarch64` plus `qemu-img` matching your guest architecture

From the repository root, install dependencies and launch the development workbench:

```bash
uv sync --locked
npm --prefix desktop ci
npm --prefix desktop run dev
```

To type-check, build for production, and preview locally:

```bash
npm --prefix desktop run build
npm --prefix desktop run preview
```

### Launcher & Environment Variables

* Palette Desktop does not bundle Python or QEMU; the launcher executes the Python backend module from the current repository using `uv`.
* Selecting a configuration directory in **Environment Settings** changes where `palette.toml`, `profiles/`, and `.runtime/` are loaded from.
* Set `PALETTE_UV` to an absolute path if `uv` is not on your system `PATH`.
* Set `PALETTE_ROOT` to override the initial configuration root directory at startup.

### Window & UI Behavior

* **Integrated Title Bar**: On Windows, the title bar blends into the workspace while preserving native minimize, maximize, and close controls. Drag the top blank area or the Palette brand header to move or snap the window; the menu bar is hidden by default.
* **Responsive Scaling & Independent Scroll**: The minimum window size is `1100 × 700` logical pixels, respecting OS display scaling. In the VM Settings view, the form body scrolls independently so the header and Save/Cancel actions remain pinned. Opening any modal dims the native window control region alongside the backdrop and restores it when the last modal closes.

## 2. Creating & Managing Persistent VMs

### VM Creation Wizard

You can create a persistent VM from two source types:

1. **Import Base Image (`qcow2` / `raw`)**: The imported file is treated strictly as a **read-only base image** (preserve its original path and any backing chain). Palette creates a writable overlay inside `.runtime/`. The configured virtual disk size cannot be smaller than the base image.
2. **Install from ISO**: Palette creates a managed blank disk of your chosen capacity and attaches the installer ISO. Firmware (BIOS / UEFI) and storage drivers must match the guest OS (e.g., Windows ISO installs may require an extra CD-ROM with VirtIO drivers).

Persistent VM definitions are stored under `<runtime_root>/definitions/`. Writable disks and per-VM UEFI variable stores persist across guest shutdowns, restarts, and new QEMU process instances.

> **CLI Ephemeral VMs vs. Desktop Persistent VMs**: Bare CLI `palette vm start` calls against ad-hoc VM IDs continue to use disposable per-run overlays. When CLI lifecycle commands target a VM ID defined in Palette Desktop, they honor its persistent disk and hardware configuration.

### Lifecycle & Restoring Initial State

* **Graceful Shutdown & Force Stop**: Normal shutdown sends an ACPI shutdown signal; if the guest does not power off within the timeout budget, Palette Desktop reports an error and leaves the VM running rather than silently killing it. **Force Stop** requires explicit confirmation.
* **Restart**: Preserves all persistent disk changes and UEFI variables.
* **Restore Initial State**: Discards all writable disk modifications and resets UEFI variables — image-backed VMs return to the clean read-only base image, and ISO-backed VMs return to a fresh blank disk.
* **Delete VM**: Removes only managed files inside `.runtime/` that have no remaining dependents. Imported base images and external ISOs are **never** deleted.

### Window Close & Exit Modes

Closing the Palette Desktop window always prompts **"Exit Palette?"** (even when no VMs exist or all VMs are stopped) with two options:

* **Close Window Only**: Closes the Electron UI while leaving the background `daemon` and any running VMs active for MCP clients.
* **Quit Completely**: Gracefully shuts down all running VMs owned by the connected backend, stops the backend owner, verifies process termination, and then closes the window (preserving VM definitions and persistent disks, and leaving other config roots untouched). If a guest shutdown or daemon stop fails, the window stays open with the error message so you can retry, cancel, or choose **Close Window Only**.

## 3. Virtual Machine Hardware Settings

Open **VM Settings** from a VM's action menu or detail view to configure hardware across six sections. **Settings are read-only while a VM is running**; stop the VM to edit and save changes for the next boot. Optimistic revision checks prevent concurrent sessions from overwriting each other, and leaving an unsaved form prompts you to save, discard, or keep editing.

| Section | Capabilities & Rules |
| --- | --- |
| **General** | View or update the VM display name and notes |
| **Processor & Memory** | **vCPUs**: `1–64` cores; **Memory**: `256–1,048,576 MiB`. Host CPU/RAM figures are shown for reference and do not reserve host capacity |
| **Disk & Boot** | **Managed Disk Expansion**: Confirmed as an independent operation (requires saving or discarding other form edits first), grow-only up to `4096 GiB`. The UI distinguishes configured capacity, actual disk virtual size, and host bytes used. Expansion modifies only the managed writable layer, leaving base images and UEFI vars untouched, and does not automatically grow guest OS partitions. Interrupted resizes recover from on-disk state and operation journals, blocking boot if integrity cannot be verified.<br>**ISO & Boot Order**: Attach, swap, or eject an ISO on both image-backed and ISO-backed VMs. Boot order supports **Automatic** (CD-ROM first on initial boot, disk first thereafter), **Disk First**, and **CD-ROM First** (requires mounted media) |
| **Display** | Dynamically filters GPU models (`VGA`, `virtio-gpu-pci`, `ramfb`) based on guest architecture and local QEMU `-device help`. Supported GPUs accept a default resolution hint (`ramfb` is controlled by firmware/guest and explains this constraint in the UI) |
| **Network** | Choose **Offline (`offline`)**, **Restricted Lab (`restricted-lab`)**, or **NAT (`nat`)**, select the NIC model, set a custom MAC address, and configure TCP/UDP port forwards. New port forwards bind strictly to `127.0.0.1` |
| **Audio** | Routes guest audio through **Intel HDA** to the host's default audio output device (**microphone capture is never enabled**). Requires a supported QEMU audio backend (`CoreAudio` on macOS, `DirectSound` on Windows, `PipeWire`/`PulseAudio`/`ALSA` on Linux) and guest HDA drivers; missing backends fail with a clear diagnostic |

> The **More Hardware Options** step in the creation wizard reuses these exact forms. Version-1 VM definitions are upgraded in place upon saving without mutating disks on read.

## 4. Live Console & Human Control Handoff

### Read-Only Display Gateway (`DisplayGateway`)

Opening the **Console** tab starts in **View Only** mode:

* Video frames stream over a localhost-only WebSocket RFB gateway authenticated by a **one-time display ticket** issued to the Electron main process.
* The gateway enforces read-only viewing at the RFB protocol layer — even if `noVNC` flags were tampered with in the renderer, input messages over the display socket are dropped.
* QEMU credentials stay in the Python backend and operator IPC credentials stay in the Electron main process. Agent `vm_observe` calls continue to use QMP screendumps with full frame-epoch validation.

### Exclusive Human Takeover (`ControlLease`)

1. Click **Take Over** in the console toolbar to acquire an exclusive human control lease for that VM (`controller_epoch` increments and all prior agent `frame_id` tokens are immediately retired).
2. Click inside the display canvas to focus and send live pointer moves, clicks, drags, wheel scrolls, and key holds. The toolbar also provides one-click `Ctrl+Alt+Del` and `Super` buttons.
3. Human input routes through the exact same single-writer input controller used by agents. While a human holds the lease, **agents can still call `vm_observe`, but any `vm_act` call is rejected with `BUSY`**.
4. **Hardware Scancodes**: Physical key presses map to guest keyboard scancodes; host IME composition strings and host clipboard contents are not bridged in this release.
5. **Releasing Control**: Press **`Ctrl+Alt`** (the legacy **`Ctrl+Alt+G`** also works), or click **End Control** to release control. Leaving the Console view, losing window focus, minimizing/hiding the window, exiting fullscreen, guest resolution changes, or display disconnects automatically release all held inputs and end the lease. If renderer heartbeats stop for **3 seconds**, the backend lease expires and releases any held keys/buttons automatically.

## 5. Shared Daemon & MCP Integration

Palette Desktop, the Operator CLI, and MCP clients share a single backend owner per `runtime_root`:

```bash
uv run palette daemon start           # start the shared background daemon
uv run palette daemon status          # inspect owner PID, kind, and generation
uv run palette serve --connect-only   # attach an MCP stdio proxy to the running daemon
uv run palette daemon stop            # stop the daemon and clean up managed QEMU processes
```

* **Automatic Discovery**: Palette Desktop attaches to an existing compatible owner if present, or spawns `palette daemon` automatically.
* **`serve` Proxy vs. Standalone**: Running `palette serve` automatically proxies to an existing `daemon` when one is found (closing the MCP client does not stop the daemon or VMs). If Palette Desktop connects to a stdio-owned `serve` process instead of a `daemon`, the UI displays that ownership mode explicitly.
* **Live Status Polling**: The **Status** view refreshes every **2 seconds**, displaying lifecycle state, `instance_id`, control owner, `action_seq`, queued tasks, and currently held keys/pointer buttons.

## 6. Verification & Automated Tests

Run the full backend and desktop verification suite:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python scripts/check_docs.py
npm --prefix desktop test
npm --prefix desktop run test:e2e
```

The Electron Playwright suite (`npm --prefix desktop run test:e2e`) runs against an isolated config root, supervised fake QEMU child processes, and real localhost RFB connections to verify VM creation, hardware settings, live console streaming, human takeover, and shutdown dialogs. For physical guest verification, follow the [acceptance test guide](acceptance-test.md).
