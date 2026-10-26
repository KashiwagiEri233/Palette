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

The conversation sidebar groups conversations under their VMs without a separate recent-conversations section. VM management lists and detail pages use the full content width beside the global rail, with no conversation sidebar. Returning to Home or a conversation restores the saved sidebar visibility and width. Settings uses its own category navigation.

The interface retains its existing component styles: pill-shaped actions, rounded form fields, larger card corners, and rounded menus. Similar controls share the same styling in light and dark themes. Selection menus use the application theme and support keyboard navigation; console scaling remains available in fullscreen. Press Escape to close an open scaling menu before exiting fullscreen.

The title bar and left icon rail share one background. VM navigation, chat, and workspace share a continuous content surface with four rounded corners, thin dividers, and aligned headers. A narrow outer gap along the right and bottom edges keeps the surrounding background visible. Powered-off screens show the start action directly; live screens retain scaling, fullscreen, and human control actions. Light and dark themes use the same structure.

Each conversation permanently belongs to one VM; selecting it changes the displayed VM. Create, rename, pin, archive, delete, export, and search are supported. Drafts persist in local application data, isolated by configuration directory. Failed saves retain the current draft with a retry action. Search includes archived conversations.

The built-in pi coding-agent SDK runs in Electron main. Under Settings → Agent, add a built-in provider with its API Key or create multiple named custom OpenAI Chat Completions, Responses, or Anthropic Messages providers. Each custom provider has its own endpoint, encrypted key and model list, even when model IDs or endpoints overlap. A vision-capable model can observe and operate the conversation's VM; text-only models support chat. Starting a visual task sends its screenshots to the selected model service. Without configuration, Home continues saving task drafts.

For a custom provider, enter its Base URL and key, then use **Get models** to search and select models to add, or add models manually. OpenAI-compatible URLs normally include the API prefix, such as `/v1`; Anthropic URLs follow the SDK convention and normally omit `/v1`. Discovery uses the newly entered key or the saved key for the unchanged endpoint; changing the endpoint or API type requires entering a key again for discovery. Fetching only creates a candidate list: save the provider to persist selected models. Existing model names and capabilities are preserved on re-import. If discovery is unsupported or fails, the form and existing models remain available for manual editing. Discovery has a 15-second timeout and a 1000-model limit. Models without returned capability metadata start with image input and reasoning disabled, a 128000-token context and an 8192-token output limit; adjust these under each model’s advanced settings before use.

The chat selector only offers providers with an application-saved API Key and at least one model. Unconfigured providers remain editable in Settings. “Configured” means a key is saved, not that authentication or model invocation has been verified; environment variables and account login are not used. Removing the selected provider, model or key clears the selection and disables sending until you explicitly choose another model, preserving history and drafts. Configuration changes are blocked while any task runs or compacts. The old single custom endpoint is automatically migrated once, retaining its key, active model, effort and tool-call limit; the legacy settings file is retained for recovery.

The chat supports streaming Markdown replies, including headings, lists, code blocks, quotes and tables, as well as Stop and manual/automatic context compaction. The model selector on the right of the composer shows the current model and effort. Open it to search models grouped by provider or choose a reasoning effort. Models without reasoning support show Off and disable the effort option. Selections are saved to the global Agent configuration; changes are disabled during a run or compaction. API keys and custom endpoints remain under Settings → Agent. Hover over or click the context ring to its left to see estimated token usage, remaining capacity and the manual compaction action. Compaction is disabled while running or for empty/read-only conversations; usage may remain unknown after compaction until the next model response. The conversation no longer shows a branch navigation button. Web links open in the system browser after validation; raw HTML and automatic remote image loading are disabled. Switching pages keeps tasks running. Human takeover stops the VM's built-in task before acquiring control. Only one Agent run may drive a VM at a time; other clients can still observe. Closing the app stops its Agent tasks while the background option keeps the daemon and VMs running. Interrupted tasks never resume automatically.

API Keys are encrypted through the operating system's secure storage; when that facility is unavailable they are kept only in memory. Conversations use pi JSONL files in the configuration-scoped application data directory. The default tool-call limit is 100, configurable from 1 to 1000. The built-in Agent directly calls optional Unicode input, guest clipboard, QGA binary transfers and Windows filesystem/PowerShell/process/registry/App/UIA/batch tools; live availability is reported per VM. See [guest enhancements](guest-enhancements.md). Automatic approvals, step replay, OAuth and third-party extension installation are not implemented.

Minimum window size is1100×700. Below1280px navigation becomes a VM icon strip; below1100 effective CSS pixels the workspace is a drawer. Drag separators or double-click to reset. Layout and light/dark/system preferences persist locally. Ctrl+N creates a conversation, Ctrl+K opens commands, Ctrl+B toggles navigation, and Ctrl+J toggles workspace; macOS uses Cmd equivalents. Ctrl+Alt releases human control, with Ctrl+Alt+G retained; the UI action is “结束控制”.

See the [GUI and built-in Agent specification](desktop-agent-design.md) for the broader target; its approval and replay interfaces remain design proposals.

To run Palette Desktop from source, install:

* **Node.js**: `22.19` or newer
* **Python & Package Manager**: Python `3.12` or `3.13` and [uv](https://docs.astral.sh/uv/)
* **QEMU**: `qemu-system-x86_64` or `qemu-system-aarch64` plus `qemu-img` matching your guest architecture

From the repository root, install dependencies and launch the development workbench:

```bash
uv sync --locked
npm --prefix desktop ci
npm --prefix desktop run dev
```

Development and production builds first compile the seven vendored pi packages from pinned sources and a checksummed model catalog. Normal builds do not read `ref/` or fetch a current model catalog. The renderer does not import the SDK. Model requests run in main; VM tools use the separately authenticated Agent IPC endpoint returned by backend `hello`, over Unix sockets or Windows named pipes. The backend must advertise `driver-claim-v1`; restart an older daemon before using visual tasks.

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

Every creation entry uses four steps: **Choose configuration → Choose source → Configure VM → Confirm creation**.

First select a **Preset** or **Custom** configuration. Presets offer Windows and Linux guests in x86_64 or ARM64; Custom retains trusted Profile selection. Guest presets are independent of host execution profiles: a Linux guest on a Windows host still uses the appropriate Windows-host accelerator. TCG requires explicit software-emulation selection. Targets without a matching Profile show a reason and never silently switch accelerators.

| Suggested configuration | Windows | Linux |
| --- | --- | --- |
| CPU / RAM / disk | 4 cores / 8 GiB / 80 GiB | 2 cores / 4 GiB / 32 GiB |
| x86_64 display / NIC | VirtIO VGA / e1000 | VirtIO VGA / VirtIO Net |
| ARM64 display / NIC | RAMFB / VirtIO Net | VirtIO GPU / VirtIO Net |

The x86_64 presets prefer `virtio-vga`; if absent from the selected QEMU, Windows uses `VGA` and Linux uses `virtio-gpu-pci`, with an explanation in the wizard. Targets with no supported candidate are unavailable. ARM64 uses `ramfb` for Windows and `virtio-gpu-pci` for Linux. These defaults apply when creating a VM or explicitly reselecting a preset; saved VMs and custom Profiles retain their settings.

Network mode, audio, storage controller and firmware inherit the base Profile. Step three exposes CPU cores, memory and disk size directly, without opening advanced settings; the final edited values are saved. Back navigation and background refresh preserve edits, and replacing a modified configuration requires confirmation. CPU/memory cannot exceed host totals, and disk size cannot be smaller than the imported image's virtual capacity; validation occurs before creation.

A preset records the intended guest family, without detecting the installed OS, installing drivers, configuring TPM or bypassing Windows setup checks. Supply media with matching architecture, firmware and drivers. For the Windows x64 tools/driver ISO and menu mounting action, see [guest enhancements](guest-enhancements.md).

Step two offers two source types:

1. **Import Base Image (`qcow2` / `raw`)**: The imported file is treated strictly as a **read-only base image** (preserve its original path and any backing chain). Palette creates a writable overlay inside `.runtime/`. The configured virtual disk size cannot be smaller than the base image.
2. **Install from ISO**: Palette creates a managed blank disk of your chosen capacity and attaches the installer ISO. Firmware (BIOS / UEFI) and storage drivers must match the guest OS (e.g., Windows ISO installs may require an extra CD-ROM with VirtIO drivers).

Persistent VM definitions are stored under `<runtime_root>/definitions/`. Writable disks and per-VM UEFI variable stores persist across guest shutdowns, restarts, and new QEMU process instances.

> **CLI Ephemeral VMs vs. Desktop Persistent VMs**: Bare CLI `palette vm start` calls against ad-hoc VM IDs continue to use disposable per-run overlays. When CLI lifecycle commands target a VM ID defined in Palette Desktop, they honor its persistent disk and hardware configuration.

### Lifecycle & Restoring Initial State

The VM management list provides **Start / Shut Down**, **Restart**, **Pause**, **VM Settings**, and **Delete VM** directly in each row. Start changes to Shut Down while the VM is active. The power button in the console, VM list, and VM menus force stops the VM after confirmation, like cutting its power. The dialog warns about unsaved data and guest filesystem corruption; cancel leaves the VM running. To shut down safely, use the guest OS or choose **Request Graceful Shutdown** from **More**. Unavailable actions are disabled. Pause is shown but is not supported yet. **More** retains Force Stop and Restore Initial State. Deleting a VM still requires confirmation.

* **Graceful Shutdown & Force Stop**: The power button uses the explicit force-stop path and does not wait for a guest shutdown request. **Request Graceful Shutdown**, restart, and **Quit Completely** use the graceful path: Palette prefers an online QEMU Guest Agent with shutdown support. It uses ACPI through QMP when the agent is unavailable or explicitly rejects the capability. An uncertain agent request is never automatically replayed. Paused guests resume first so they can handle the request. The console keeps displaying the guest during shutdown without a covering placeholder; manual input is released and takeover is disabled.
  The shutdown budget covers command dispatch, the guest-agent lock, and waiting for process exit. A timeout or channel failure displays an error without silently killing a running guest. **Force Stop…** is available during the wait and after failure, and requires separate confirmation. Cancelling that confirmation leaves the original graceful wait active. Confirmed force stop can interrupt the wait immediately, and cancels any pending restart or restore so it cannot create a replacement instance afterward.
  Confirmation is tied to the original instance: if another client has restarted the VM, Palette refuses to stop its replacement. Force stop can terminate the owned process immediately to break a blocked shutdown; otherwise it first asks QEMU to quit before terminating the process and its guard if necessary. Only confirmed process exit is reported as **Stopped**; disks and UEFI variables remain intact. Failed instances with live processes can still be force stopped and are not treated as exited. Enhancement-channel closing and file-handle cleanup are bounded so they cannot hold shutdown indefinitely. A normal shutdown initiated inside the guest is recognized as **Stopped** once QEMU emits a guest shutdown event and exits successfully; a crash remains a failure.
* **Restart**: Preserves all persistent disk changes and UEFI variables.
* **Restore Initial State**: Discards managed system disk modifications and resets UEFI variables — image-backed VMs return to the clean read-only base image, and ISO-backed VMs return to a fresh blank disk. Additional disk and optical image files retain their contents.
* **Delete VM**: Removes only managed files inside `.runtime/` that have no remaining dependents. Imported base images and external ISOs are **never** deleted.

### Window Close & Exit Modes

Closing the Palette Desktop window always prompts **"Exit Palette?"** (even when no VMs exist or all VMs are stopped) with two options:

* **Close Window Only**: Closes the Electron UI while leaving the background `daemon` and any running VMs active for MCP clients.
* **Quit Completely**: Independently requests graceful shutdown for each active VM owned by the connected backend and waits for all results, stops the backend owner, verifies process termination, and then closes the window (preserving VM definitions and persistent disks, and leaving other config roots untouched). If a guest shutdown or daemon stop fails, the window stays open with the error message so you can retry, cancel, or choose **Close Window Only**.

If a VM operation is already in progress, **Quit Completely** asks you to wait instead of queuing another shutdown. You can still cancel or choose **Close Window Only**. After a shutdown failure, VM status refreshes even while the exit dialog is open; force stopping remains a separate confirmed action.

When a guest times out or its shutdown channel fails during **Quit Completely**, **Force Stop This VM and Quit…** opens a separate confirmation naming the affected VM and warning about unsaved data. After confirmation, Palette stops that instance, continues graceful shutdown for the remaining VMs, and verifies backend termination. Cancelling the confirmation leaves the affected VM running. This recovery requires an updated backend advertising `vm-stop-identity`.

ACPI depends on the guest's power-button handling; QEMU Guest Agent depends on a service inside the guest. Neither guarantees that the guest will complete shutdown. Palette checks actual QEMU process exit; a blank display is not evidence of shutdown.

## 3. Virtual Machine Hardware Settings

On Windows, ISO, disk and firmware paths containing non-ASCII characters are loaded through a QEMU drive configuration encoded in the system code page. This avoids path corruption between command-line/JSON handling and Windows file APIs, while retaining read-only ISO access and explicit disk formats. Characters outside that code page produce a clear error before launch. This compatibility path does not establish support for arbitrary Unicode runtime directories or image backing chains.

Open **VM Settings** directly from the VM management row, or from the VM's action menu or detail view, to configure hardware across six sections. **Settings are read-only while a VM is running**; stop the VM to edit and save changes for the next boot. Optimistic revision checks prevent concurrent sessions from overwriting each other, and leaving an unsaved form prompts you to save, discard, or keep editing.

| Section | Capabilities & Rules |
| --- | --- |
| **General** | View or update the VM display name and notes |
| **Processor & Memory** | **vCPUs**: `1–64` cores; **Memory**: `256–1,048,576 MiB`. Host CPU/RAM figures are shown for reference and do not reserve host capacity |
| **Boot** | Select any optical drive with connected media for boot. Boot order supports **Automatic** (CD-ROM first on initial boot, disks first thereafter), **Disk First**, and **CD-ROM First** (requires mounted media). Drive order controls priority within each media type |
| **Display** | Dynamically filters GPU models (`VGA`, `virtio-vga`, `virtio-gpu-pci`, `ramfb`) based on guest architecture and local QEMU `-device help`. Supported GPUs accept a default resolution hint (`ramfb` is controlled by firmware/guest and explains this constraint in the UI) |
| **Network** | Choose **Offline (`offline`)**, **Restricted Lab (`restricted-lab`)**, or **NAT (`nat`)**, select the NIC model, set a custom MAC address, and configure TCP/UDP port forwards. New port forwards bind strictly to `127.0.0.1` |
| **Audio** | Routes guest audio through **Intel HDA** to the host's default audio output device (**microphone capture is never enabled**). Requires a supported QEMU audio backend (`CoreAudio` on macOS, `DirectSound` on Windows, `PipeWire`/`PulseAudio`/`ALSA` on Linux) and guest HDA drivers; missing backends fail with a clear diagnostic |

The settings sidebar groups **Devices** and **Drives**, with one entry per drive. Select an entry to edit it; its action menu offers **Move Up**, **Move Down**, and **Delete drive…**. Order, interfaces and other edits share one draft, preserved through selection changes and background refresh.

If the connected backend has not loaded the current drive-management code, settings ask you to restart the backend and reconnect. Missing capability information is not reported as QEMU lacking storage interfaces.

The disk created with the VM is an ordinary entry in this list. Edit its interface, access mode and connection state, reorder it, or delete it using the same actions as other drives. Save commits deletion; Cancel preserves its contents. Removing the original disk does not remove the VM or source image, and subsequent starts or Restore Initial State do not recreate it. A VM can have no hard disks and boot from optical media.

**New…** opens a configuration dialog for **Removable media**, **Interface**, and, for ordinary disks, **Capacity / GiB** and **Image format (QCOW2 / RAW)**. Choose **Create** for a blank disk or **Import…** for an existing image. Removable media creates an ordinary optical drive: **Browse…** selects an external ISO, while **Create** can leave it empty. Cancelling the dialog or file picker adds nothing. Files are created or imported only when settings are saved.

Disk interfaces include VirtIO, IDE, SCSI, NVMe and USB; optical drives support IDE, SCSI and USB. The local QEMU device list, architecture and machine filter the actual choices. ARM64 does not offer PC IDE; VirtIO Block and NVMe are not optical interfaces. Guests require matching drivers, and IDE count is limited by machine ports. Saved order controls device construction and boot priority within the disk/optical groups; the selected optical drive takes priority within its group.

Ordinary disks belong to the VM. Import creates an independent copy in the selected format inside the VM directory, flattening its backing chain and preserving the source. Guest writes modify the copy. Blank disks accept `1–4096 GiB`; imports preserve the actual source virtual capacity, up to `4096 GiB`. Details show name, format, virtual capacity and path. Allocated disk capacity and format cannot be edited directly. Restarting and restoring the system disk preserve data disks.

Each managed disk has an **Expand disk** action, including the original disk and added RAW/QCOW2 disks. Expansion is a separate, grow-only operation up to `4096 GiB`; save or discard other edits first. It preserves source images and UEFI variables, and does not grow guest partitions automatically. Interrupted operations recover from an on-disk journal and block boot if image integrity cannot be verified.

**Delete drive…** removes the entry from the draft after confirmation. **Save** deletes the owned image; **Cancel / Discard** preserves the device and data. External ISOs and previously attached external disks only lose their device entry; their files remain. The backend checks VM, image-chain and configuration references before committing settings and cleaning files. Failed cleanup retains a pending record under **Boot → Unused disks**, where deletion can be staged and saved again. Unsaved edits never delete files, and missing disks are never silently recreated.

Configure up to nine drives in total, including the original disk, while stopped; running VMs are read-only. Interface selection and reordering preserve device IDs. Creation and deletion use the settings dialog’s Save/Cancel flow, without a separate immediate file-deletion action.

The creation ISO and all other ISOs are ordinary optical-drive media with the same sidebar editor, replacement, eject and removal actions. There is no separate installation-media setting. Disconnecting a disk, ejecting an ISO, or removing an optical drive preserves the original file. CD-ROM First requires connected media; ejecting the last available medium resets the UI boot order to Automatic. Extra NICs are not supported yet.

> The **More Hardware Options** step in the creation wizard reuses the display, network, audio and boot forms. Additional drives are configured in VM settings. Version-1 VM definitions are upgraded in place upon saving without mutating disks on read.

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

An optional VirtIO VGA test boots a disposable BIOS guest with real x86_64 QEMU. For example, in PowerShell:

```powershell
$env:PALETTE_VIRTIO_VGA_QEMU = 'C:/Program Files/qemu/qemu-system-x86_64.exe'
uv run pytest tests/e2e/test_virtio_vga.py
```

It verifies resolution parameter acceptance, framebuffer capture, authenticated VNC, keyboard/tablet display bindings, and a visible response to keyboard input. The test uses TCG and disables SMM for its minimal BIOS guest; it does not establish Windows/Linux driver or guest desktop resolution acceptance.

For real QEMU multiple-drive and tools-hotplug verification, set `PALETTE_DRIVES_QEMU` to the same executable and run `uv run pytest tests/e2e/test_real_drives.py`; sibling `qemu-img` and `qemu-io` executables are required. It checks writable disks, shared read-only backing files, loaded/empty optical drives, tools drive creation/reuse/ejection, and preservation of other media. An offline write/read pattern verifies managed data disk contents survive restart and system disk reset. This firmware-only test does not verify Windows driver installation.

## Optional Guest Enhancements

See the [guest enhancement guide](guest-enhancements.md) for guest installation, automatic capability detection and degradation, Unicode clipboard, automatic resolution, QGA management, file transfer, and private WebDAV shared directories. Missing tools preserve the basic VM console.

For real QEMU shutdown recovery verification, set `PALETTE_SHUTDOWN_QEMU` to the absolute path of `qemu-system-x86_64` and run `uv run pytest tests/e2e/test_real_shutdown.py`. It checks pause/resume, explicit force stop during a graceful wait, stopping after QMP disconnect, and disk preservation. The disposable firmware guest does not establish acceptance of Windows guest services or ACPI shutdown.
