# End-to-End Acceptance Testing Guide (MCP, Agent & Desktop)

**English** | [简体中文](acceptance-test.zh-CN.md)

This runbook verifies **MCP visual observation (`vm_observe`)**, **bounded agent clicks (`vm_act`)**, **Palette Desktop live RFB streaming**, **exclusive human takeover (`BUSY` / `STALE_FRAME`)**, and **offline VM hardware settings** against a single shared virtual machine.

The primary workflow uses the shared background daemon (`palette daemon`); the MCP client, Operator CLI, and Palette Desktop must all point to the same `CONFIG_ROOT` and `runtime_root`.

## 1. Environment Setup & Evidence Conventions

From the repository root, sync Python dependencies and build Palette Desktop:

```bash
uv sync --locked
npm --prefix desktop ci
npm --prefix desktop run build
```

* **Prerequisites**: Python `3.12`/`3.13`, `uv`, Node.js `22.19+`, a QEMU binary and firmware matching the guest ISA, and a bootable desktop base image (`qcow2`/`raw`) or installer ISO.
* **Identifiers**: Below, `CONFIG_ROOT` denotes the directory holding `palette.toml` and `profiles/`. Always copy `VM_ID` from the GUI **Status** view (e.g., `vm_489cdb67aca0`), never the human-readable display name.
* **Evidence Storage**: Store test reports and screenshots under `.runtime/acceptance/`. Never commit disk images, screenshots, or connection tokens to Git.

## 2. Start & Verify the Shared Daemon

```bash
uv run python -m palette.cli --root CONFIG_ROOT config show
uv run python -m palette.cli --root CONFIG_ROOT doctor --json
uv run python -m palette.cli --root CONFIG_ROOT daemon start
uv run python -m palette.cli --root CONFIG_ROOT daemon status
```

**Pass Criteria**:

* `doctor` reports `ok = true` (proving host launch prerequisites, though not guest desktop readiness).
* `daemon status` reports `kind = "daemon"`. Record its `pid`, `generation`, `config_root`, and `runtime_root`.
* If an older stdio-owned `serve` session already holds `runtime_root`, close that session cleanly before starting `daemon`.

## 3. Create & Boot the Test VM in Palette Desktop

Launch Palette Desktop pointing at `CONFIG_ROOT`:

```bash
# macOS / Linux
PALETTE_ROOT="CONFIG_ROOT" npm --prefix desktop run preview
```

```powershell
# Windows PowerShell
$env:PALETTE_ROOT = "CONFIG_ROOT"
npm --prefix desktop run preview
```

1. In **Environment Settings**, confirm that `config_root` and `runtime_root` match Step 2.
2. Create a VM from a `qcow2`/`raw` base image or installer ISO, select the matching profile, configure vCPUs, RAM, disk size, and networking, and record the generated `VM_ID`.
3. Start the VM, open the **Console** tab, and remain in **View Only** mode. Wait until the guest finishes booting into a usable desktop (do not treat a UEFI splash screen, login prompt, or `RUNNING` state alone as desktop-ready).
4. Open the **Status** tab and record `instance_id`, `action_seq`, control owner, epochs, display size, and held inputs (Status polls every 2 seconds while visible; the Console streams continuously over RFB).

> **Note**: Seeing "Guest Desktop: Unconfirmed" (`desktop_ready: null`) in Status is expected when no in-guest enhancement service is running. Switching away from the Console tab automatically releases human input, so stay on the Console tab while testing held human keys.

## 4. Connect the MCP Client

Configure an MCP client that supports `stdio` and forwards `ImageContent` blocks to the model (replace the three paths with absolute paths on your host):

```json
{
  "mcpServers": {
    "palette": {
      "command": "/absolute/path/to/uv",
      "args": [
        "run", "--project", "/absolute/path/to/Palette",
        "python", "-m", "palette.cli",
        "--root", "/absolute/path/to/CONFIG_ROOT",
        "serve", "--connect-only"
      ]
    }
  }
}
```

**Pass Criteria**:

* Initialization succeeds and exposes `vm_list`, `vm_status`, `vm_observe`, `vm_act`, `vm_wait`, and `vm_release_inputs`.
* `vm_list({})` discovers persistent and runtime-known VM IDs, including stopped definitions, without starting VMs, acquiring control or returning host configuration.
* `vm_status({"vm_id": "VM_ID"})` returns `ok = true`, `state = "RUNNING"`, and the exact same `instance_id` shown in the GUI.
* `vm_observe({"vm_id": "VM_ID"})` delivers an actual PNG image to the agent, not just JSON metadata.

## 5. Agent Visual Click Verification

Pick a reversible target with an obvious visual result inside the guest (e.g., double-clicking "This PC" in WinPE to open Explorer, clicking the Start menu, or opening `guest/fixtures/offline-workbench.html` inside the guest browser and clicking a grid cell).

You can give the visual agent this exact prompt (replace `VM_ID`):

> Run a visual click test on virtual machine `VM_ID`:
> 1. Call `vm_status` to confirm it is running and control is available;
> 2. Call `vm_observe` and choose a reversible target such as "This PC" or a test grid cell from the actual screenshot you received;
> 3. Using the `instance_id` and `frame_id` from that latest frame plus a fresh `request_id`, call `vm_act` once (`count = 2` if double-clicking);
> 4. Inspect the returned post-action screenshot to confirm a visible change occurred, calling `vm_observe` again if needed;
> 5. Never guess coordinates, never replay an action with an unknown outcome, and do not shut down, reset, or delete anything;
> 6. Report the before/after `instance_id`, `action_seq`, receipt, visual change, and held-input release state.

Expected MCP call sequence:

```json
{"name": "vm_observe", "arguments": {"vm_id": "VM_ID"}}
```

```json
{
  "name": "vm_act",
  "arguments": {
    "vm_id": "VM_ID",
    "instance_id": "FROM_LATEST_FRAME",
    "frame_id": "FROM_LATEST_FRAME",
    "request_id": "acceptance-click-unique-001",
    "action": {"type": "click", "x": 100, "y": 200, "button": "left", "count": 1}
  }
}
```

**Pass Criteria**:

* `vm_act` returns `ok = true`, `effect_state = "submitted"`, and a fresh screenshot + `frame_id`.
* Both the post-action screenshot and the GUI Console show the visible change in the guest.
* `action_seq` increments in both `vm_status` and the GUI Status view, and held keys/buttons end empty.
* Disconnecting the MCP proxy leaves the VM and `daemon` running with the same PID and `generation`.

## 6. Scripted MCP Observe & Click Harness

Use `scripts/mcp_click_test.py` to exercise MCP observation and clicking deterministically against the running owner:

```bash
# 1. Observe only and save 02-before.png to choose target pixel coordinates (X, Y)
uv run python scripts/mcp_click_test.py   --root CONFIG_ROOT --vm-id VM_ID --out .runtime/acceptance/observe

# 2. Execute one bounded click (add --count 2 for double-click or --button right for context menu)
uv run python scripts/mcp_click_test.py   --root CONFIG_ROOT --vm-id VM_ID --x X --y Y   --out .runtime/acceptance/click
```

The script writes `report.json`, `02-before.png`, `03-click.png`, and (after a default 1s `vm_wait`, configurable via `--wait-ms` up to 10s) `04-verification.png`. Exit code `0` indicates pass; `4` indicates failure.

## 7. Human Takeover & Contention Test

1. Click **Take Over** in the Palette Desktop Console and keep focus on the Console view.
2. From a second client (or delayed command), verify that `vm_observe` still succeeds while `vm_act` is rejected with `BUSY`:
   ```bash
   uv run python scripts/mcp_click_test.py      --root CONFIG_ROOT --vm-id VM_ID --x X --y Y --expect-error BUSY      --out .runtime/acceptance/takeover
   ```
3. Hold a key, drag, and scroll inside the Console canvas; verify via `vm_status` from another client that held inputs are tracked while pressed.
4. Click **Return to Agent** or press **`Ctrl+Alt+G`**: confirm that a fresh `vm_observe` + `vm_act` succeeds, while attempting `vm_act` with a pre-takeover `frame_id` fails with `STALE_FRAME`.
5. Verify that losing window focus, switching tabs, hiding the window, disconnecting RFB, or pausing heartbeats for **3 seconds** automatically expires the lease and releases all held inputs.
6. Drag beyond each canvas edge and release: the pointer clamps to the edge, and no button remains held. Try tiny touchpad deltas, horizontal/diagonal scrolling, double clicks and modifier chords; small deltas accumulate rather than becoming a full wheel step each.
7. Resize repeatedly with automatic resolution enabled: synchronization does not overlap, the old drag is cancelled and stale coordinates are never replayed. Confirm that an idle takeover remains usable after more than two seconds, and that an input-channel disconnect releases held input without replaying it.
8. Run the same interactions while a second client repeatedly observes. Collect the separate input/ACK/visible-response and frame-interval metrics described in [Benchmarks](benchmarks.md), then run the 30-minute endurance check on a real guest. Simulated tests do not establish hardware performance acceptance.

## 8. Regression & Shutdown Matrix

| Test Case | Action | Pass Condition |
| --- | --- | --- |
| **Stale frame** | Reuse a pre-action `frame_id` after a successful action | Rejected with `STALE_FRAME`; no guest side effect |
| **Idempotent retry** | Resend identical `request_id` and parameters | Returns cached receipt (`deduplicated = true`) without clicking twice; altered parameters return `REQUEST_ID_CONFLICT` |
| **Scale mapping** | Click the same target under fit-to-window, 1:1, and high-DPI scaling | Lands on the exact target in both MCP (image pixels) and GUI (mapped coordinates) |
| **Fullscreen** | Enter and exit Console fullscreen | Stream remains intact; exiting fullscreen releases human control |
| **Hardware restart** | Click Restart in the console, list and menu while the guest ignores ACPI | No confirmation or shutdown wait; only `system_reset`; process and `instance_id` unchanged, `boot_epoch` advances; disks and UEFI vars preserved; old frames and leases retired; view-only mode restored |
| **View-only pointer** | Hover the display after initial connection, ending control, focus loss and reconnect; cover windowed and fullscreen modes | Host pointer stays visible and no guest input is sent; takeover retains guest cursor behavior |
| **Persistent stop/start** | Shut down gracefully and start the VM again | New `instance_id`; persistent disk and UEFI vars preserved; prior frames retired |
| **Power button** | Click Shut Down in the console, VM list, and VM menu; cancel once, then confirm using a disposable guest that ignores ACPI | Confirmation names the VM and warns about unsaved data and filesystem corruption; cancel preserves the running instance; confirm force stops it without an ACPI request and retains persistent disks |
| **Close Window Only** | Close the GUI window and choose **Close Window Only** | `daemon` and VM stay running; MCP continues observing; reopening GUI reconnects to the same instance |
| **Quit Completely** | Close the GUI window and choose **Quit Completely** | Gracefully shuts down running VMs, stops `daemon`, verifies process exit, and closes the window while preserving persistent disks |
| **Shutdown failure** | Trigger **Quit Completely** when ACPI shutdown times out | Window remains open displaying the error with options to retry, cancel, or close window only |
| **Confirmed recovery** | After a shutdown timeout, choose **Force Stop This VM and Quit…**; cancel once, then explicitly confirm | Cancel preserves the VM; confirmation stops only that instance, preserves disks, and continues graceful exit for other VMs. A replacement instance is rejected |
| **Guest-initiated shutdown** | Shut down from inside the guest | A guest `SHUTDOWN` event followed by QEMU exit code 0 becomes `STOPPED`, releases resources, and retains persistent disks; panic or nonzero exit remains `FAILED` |

Record QMP events, process exit code, elapsed time, and whether force was used. `POWERDOWN` only establishes that the power request was sent; it is not proof of a completed shutdown. A CLI stop that times out and then kills QEMU must not count as a graceful shutdown pass. Compare guest-agent shutdown and ACPI shutdown separately.

## 9. Offline Hardware Settings Verification

Using a disposable real guest VM while stopped, verify each hardware setting category (confirming the UI is read-only and the backend rejects mutations while running):

1. **vCPUs & Memory**: Edit vCPU count and RAM while stopped, boot, and verify inside the guest and via QMP.
2. **Managed Disk Expansion**: Write a test file inside the guest, shut down, expand the disk, verify virtual size growth and byte-for-byte preservation of the test file and UEFI vars, then expand the guest partition.
3. **Display & Default Resolution**: Test resolution hints on supported GPUs (`virtio-gpu-pci`) and confirm `ramfb` explains its firmware-controlled resolution constraint.
4. **ISO & Boot Order**: Swap/eject ISOs and verify Automatic, Disk First, and CD-ROM First boot preferences.
5. **Networking & Port Forwards**: Test `offline`, `restricted-lab`, and `nat`; verify port forwards bind strictly to host `127.0.0.1`.
6. **Intel HDA Audio**: Enable audio, verify host audio backend initialization (`CoreAudio` / `DirectSound` / `PipeWire`), confirm playback, and verify no microphone capture occurs.

## 10. Physical Host Acceptance Records (macOS ARM64 + HVF)

### 10.1 2026-10-13 ARM64 WinPE Visual & Takeover Record

* **Host**: macOS ARM64, QEMU `11.1.2` (`HVF`), `virt,gic-version=3,its=off` + UEFI + `ramfb`, 2 vCPUs / 4 GiB RAM / 8 GiB disk / `offline` network.
* **Media**: `CNBYDJ_PE_V4.0.0.ISO` (SHA-256: `11bc4c89e6529febc07beb60768c4bab2f3167c739ada9182547095363f2f61d`), isolated root `.runtime/winpe-acceptance/`, VM `vm_489cdb67aca0`, instance `inst_90f74625c80b`, display `800 × 600`.

| Check | Result & Evidence |
| --- | --- |
| **WinPE Boot to Desktop** | **PASS**: Booted via CD prompt / `fs1:\EFI\BOOT\BOOTAA64.EFI` into the interactive WinPE desktop |
| **MCP Init & Capture** | **PASS**: Standard `stdio` proxy attached to shared `daemon`; verified `vm_status` and `vm_observe` PNG delivery |
| **Double-Click "This PC"** | **PASS**: `889.70 ms` end-to-end call; subsequent capture confirmed Explorer window opened |
| **Right-Click Context Menu** | **PASS**: `273.42 ms` end-to-end call; subsequent capture confirmed Explorer context menu visible |
| **GUI Live Sync** | **PASS**: `action_seq` updated from `11` to `12` on the same instance; live RFB canvas reflected window state |
| **Human Takeover & BUSY** | **PASS**: MCP `vm_observe` succeeded during takeover, `vm_act` returned `BUSY`; releasing control cleared held inputs with `release_confirmed = true` |
| **MCP Proxy Disconnect** | **PASS**: VM and `daemon` stayed up across multiple MCP client disconnects |

### 10.2 2026-10-14 Hardware Settings & Disk Expansion Record

* **Host**: macOS `27.0.1` / ARM64, QEMU `11.1.2` / `HVF`.
* **Results**:
  * Real `qemu-img` expanded the managed writable disk from `8 GiB` to `12 GiB`; `qemu-io` confirmed a 4096-byte pattern at offset `1048576` and UEFI variable bytes remained intact.
  * QMP and process arguments confirmed `2` vCPUs, `1024 MiB` RAM, VirtIO NIC, `127.0.0.1`-only port forwarding, and Intel HDA audio with `CoreAudio` initialization.

## 11. Acceptance Report Template & Automated Regression Commands

Record each physical acceptance pass using this template:

```text
Date / Host OS / Arch:
Git SHA / Python / QEMU / Accelerator:
ISO or Base Image Version / SHA-256:
CONFIG_ROOT / runtime_root / daemon PID / generation:
VM_ID / instance_id / Guest Display Size:
MCP Client / Model / ImageContent Verified:
Click Target / Image Coordinates / request_id / Before & After action_seq:
Action Receipt / Guest Visual Outcome / GUI Sync Outcome:
Human Takeover / BUSY Rejection / STALE_FRAME Rejection / Release Confirmation:
Graceful Shutdown / Proxy Disconnect / GUI Exit Outcome:
Timing Definition / Sample Count / Screenshot & report.json Paths:
Passed, Failed, or Skipped Items & Notes:
```

Automated regression suite:

```bash
uv run pytest tests/integration/test_desktop.py tests/integration/test_vm_settings.py tests/unit/test_display.py
uv run python scripts/check_docs.py
npm --prefix desktop test
npm --prefix desktop run test:e2e
```

### 11.1 Windows QMP restart acceptance

Run the native transport regression with `uv run python -m pytest tests/unit/test_windows_unix.py tests/unit/test_guest_pipe_startup.py tests/qmp tests/integration/test_qmp_startup.py`. It includes a deterministic reproduction of the old early-client `ConnectNamedPipe` race without blocking the test process.

For real QEMU, configure the following in PowerShell using a dedicated Alpine test ISO and its extracted kernel/initramfs:

```powershell
$env:PALETTE_QMP_QEMU = 'C:/Program Files/qemu/qemu-system-x86_64.exe'
$env:PALETTE_QMP_ACCEL = 'whpx'
$env:PALETTE_QMP_ALPINE_ISO = 'C:/test-media/alpine-virt-x86_64.iso'
$env:PALETTE_QMP_KERNEL = 'C:/test-media/boot/vmlinuz-virt'
$env:PALETTE_QMP_INITRD = 'C:/test-media/boot/initramfs-virt'
$env:PALETTE_QMP_CYCLES = '20'
uv run python -m pytest tests/e2e/test_qmp_restart.py -s -o junit_family=xunit1 --junitxml=.runtime/qmp-restart.xml
```

The firmware checks start QEMU with four enhancement channels, including a run with every host peer deliberately absent. The persistent-guest check uses the desktop backend's normal shutdown path for 20 cycles, then guest-initiated poweroff and explicit force stop. It verifies instance replacement, private endpoint cleanup, reuse of the persistent writable disk, a disk pattern across starts and the unchanged source ISO. A force stop or QMP `quit` does not count as normal shutdown. The ISO is only read; the test creates its own writable VM in pytest's temporary directory. WHPX is the primary Windows gate; repeat with `PALETTE_QMP_ACCEL=tcg` for supplementary coverage. Missing environment variables skip hardware tests and establish no physical acceptance.

### 11.2 2026-10-28 Windows QMP shutdown/restart record

- **Host and guest**: Windows 11 build `26300`, Python `3.12.13`, QEMU `11.1.0` (`v11.1.0-12130-ge470268ff4`); disposable Alpine `3.22.0` guest, `q35`, 1 vCPU, 512 MiB RAM, 1 GiB persistent writable disk, offline network. QGA, tools, shares and session channels were all enabled.
- **WHPX: PASS**: 20 normal ACPI shutdown/start cycles, followed by guest-initiated poweroff/start and explicit force stop. Every cycle verified process exit, new instance identity, reuse and contents of the writable disk, endpoint removal and an unchanged source ISO. This validates the host QMP/transport lifecycle with a real Linux guest.
- **TCG: PASS**: one normal ACPI shutdown cycle plus guest-initiated poweroff and force stop; the same disk and endpoint checks passed.
- **Transport/recovery: PASS**: repeated firmware starts with all four peers attached or deliberately absent, real QEMU Unicode/space endpoints, and injected startup failure followed by a successful fresh start. The native Win32 early-client pipe race was reproduced deterministically; the socket transport avoided it.
- **Automated backend regression**: 979 passed, 12 platform-dependent skips, no failures. Ruff lint/format, strict mypy and paired documentation checks passed. Local JUnit artifacts are retained under `.runtime/qmp-*.xml`.

### 11.3 2026-10-28 Windows QMP concurrency record

- **Reproduction**: the Windows stdio monitor produced uncorrelated JSON parse errors and 3 command timeouts with only 8 concurrent valid status queries. The Windows QMP transport now uses a separate current-user-only AF_UNIX socket; POSIX keeps child stdio. The command timeout and no-replay policy are unchanged.
- **WHPX and TCG: PASS**: 500 lease-bound input batches alongside 1600 status queries (32 queries in flight), with no protocol errors or held inputs afterward. Separate WHPX probes at 8, 32 and 128 concurrent status queries also completed without errors.
- **Lifecycle/display: PASS**: the AF_UNIX QMP transport passed another 20 normal ACPI shutdown/start cycles, guest poweroff, force stop, failure/retry, absent guest peers, Unicode endpoints, and the existing real keyboard/framebuffer/VNC and Chinese media-path tests.
- **Backend regression**: 984 passed, 12 platform-dependent skips. The old stdout-specific disconnect injection was updated to target the actual QMP reader and its entire lifecycle regression module passed again. The aggregated report records that rerun. Evidence: `.runtime/qmp-socket-backend-regression.xml`, `.runtime/qmp-concurrency-whpx.xml`, `.runtime/qmp-concurrency-tcg.xml`, `.runtime/qmp-socket-restart-whpx.xml` and `.runtime/qmp-socket-display-paths.xml`.
