# MCP, Agent and Desktop acceptance test

**English** | [简体中文](acceptance-test.zh-CN.md)

This procedure verifies MCP observation, Agent clicks, continuous GUI display and shared
status on one VM. Use a persistent daemon. MCP and Desktop must use the same configuration
and runtime roots.

## 1. Prerequisites and evidence

Run from the repository root:

```bash
uv sync --locked
npm --prefix desktop ci
npm --prefix desktop run build
```

Install Python 3.12/3.13, uv, Node.js 22.12+, QEMU and firmware matching the guest
architecture. Native Apple Silicon uses aarch64/HVF; select TCG explicitly for emulation.
Prepare a bootable desktop disk or an ISO-based VM. Record fake-QEMU and real-QEMU results
separately: a fake test never proves that Windows/WinPE reached its desktop.

`CONFIG_ROOT` below contains `palette.toml` and `profiles/`. Copy `VM_ID` from the GUI's
“状态” tab; the display name is not the identifier. Save evidence under the runtime root or
`.runtime/acceptance/`. Do not commit disks, screenshots or connection credentials.

## 2. Start the shared owner

```bash
uv run python -m palette.cli --root CONFIG_ROOT config show
uv run python -m palette.cli --root CONFIG_ROOT doctor --json
uv run python -m palette.cli --root CONFIG_ROOT daemon start
uv run python -m palette.cli --root CONFIG_ROOT daemon status
```

Expect doctor `ok=true` and owner kind `daemon`. Record PID, generation, `config_root` and
`runtime_root`. Doctor checks prerequisites; it does not prove a usable guest desktop.
If a stdio owner already owns this runtime, end its MCP session normally before starting
the daemon. Do not launch a second `--standalone` owner. You do not need to keep a manual
`serve` process open in a terminal.

## 3. Create and start the VM in Desktop

```bash
PALETTE_ROOT="CONFIG_ROOT" npm --prefix desktop run preview
```

In PowerShell:

```powershell
$env:PALETTE_ROOT = "CONFIG_ROOT"
npm --prefix desktop run preview
```

1. Check configuration and runtime directories in “环境设置” against step 2.
2. Create a qcow2/raw or ISO VM using the matching architecture/profile, CPU, memory,
   disk and network settings. Record its VM ID.
3. Start and open “控制台”; remain in “仅查看”. Wait for guest startup. UEFI, a login
   screen or `RUNNING` alone does not prove desktop readiness.
4. In “状态”, record instance, action sequence, control owner, epochs, geometry and held
   inputs. Visible windows poll status every two seconds. Display updates use a separate
   continuous RFB connection.
5. Return to “控制台” to watch the display. Leaving it releases human input, so do not
   switch to “状态” during a held-key test.

“客户机桌面：尚未确认” means no desktop-readiness evidence was supplied; observation and
input can still work. “控制权：Agent” means Agent writes are permitted, not that a particular
MCP client is connected. Connection errors invalidate freshness assumptions; a retained
status snapshot is not new evidence.

## 4. Connect an MCP client

Use a client supporting stdio MCP and forwarding images to the model. Replace the absolute
uv executable, repository and configuration paths:

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

Expect initialization and at least `vm_status`, `vm_observe`, `vm_act`, `vm_wait` and
`vm_release_inputs`. `--connect-only` attaches without creating an owner; a missing owner
must fail. Each MCP client starts its own stdio proxy.

Call `vm_status({"vm_id":"VM_ID"})`: require `ok=true`, `state=RUNNING` and the same
`instance_id` as Desktop. Call `vm_observe` and verify that the Agent receives the actual
image. Tool discovery or an `ImageContent` metadata block alone is insufficient.

## 5. Agent click acceptance

Choose a reversible target with an obvious result, such as double-clicking WinPE's This PC
to open Explorer, or a working Start menu. Alternatively,
copy `guest/fixtures/offline-workbench.html` into the guest and open it in a guest browser;
click a grid cell and check its color/counter. Host paths and host `localhost` do not identify
resources inside the guest.

Give the Agent this task, replacing its VM ID:

> Perform one visual click test on VM_ID. Check vm_status for a running VM with control
> available. Call vm_observe and choose This PC or a test-grid cell from the actual
> latest screenshot. Call vm_act once with that frame's instance_id and frame_id, a new
> request_id, and the observed image coordinates. Use count=2 for a double-click target.
> Verify the returned screenshot and
> observe again if needed. Do not guess coordinates, replay an uncertain action, shut down,
> reset, install or delete anything. Report instance, action sequence, receipt, visible
> effect and released keyboard/pointer state.

First observe:

```json
{"name":"vm_observe","arguments":{"vm_id":"VM_ID"}}
```

Use identifiers from `frame` and pixels in its delivered `image_size`, not scaled GUI CSS
coordinates. Then act:

```json
{
  "name":"vm_act",
  "arguments":{
    "vm_id":"VM_ID",
    "instance_id":"FROM_LATEST_FRAME",
    "frame_id":"FROM_LATEST_FRAME",
    "request_id":"acceptance-click-unique-001",
    "action":{"type":"click","x":100,"y":200,"button":"left"}
  }
}
```

`100,200` only illustrates the format; choose actual coordinates from the screenshot.
Pass criteria:

- Receipt `ok=true`, `effect_state=submitted`, and a fresh screenshot/frame.
- Visible change at the intended guest target; the GUI console shows the same change.
- Matching instance in GUI/MCP, increased action sequence and eventually empty key/button
  ledgers.
- Disconnecting the MCP proxy leaves the VM running and the daemon PID/generation unchanged.

A receipt proves submission, not an application effect. If the display does not verify the
target change, record failure or uncertainty rather than passing solely on `ok=true`.

## 6. Executable MCP probe

The script attaches to an existing owner; it never starts, resets or stops a VM. Observe first:

```bash
uv run python scripts/mcp_click_test.py \
  --root CONFIG_ROOT --vm-id VM_ID --out .runtime/acceptance/observe
```

Inspect `02-before.png`, select a reversible target and confirm that the guest layout has
not changed. Run one click; the script captures a fresh frame itself. Re-select coordinates
if the layout changed:

```bash
uv run python scripts/mcp_click_test.py \
  --root CONFIG_ROOT --vm-id VM_ID --x X --y Y \
  --out .runtime/acceptance/click
```

Outputs include `report.json`, the pre-click image and `03-click.png` on successful capture.
By default, `vm_wait` waits one second and saves `04-verification.png`. Set `--wait-ms 2000`
to adjust, up to ten seconds. If the guest is still responding, observe without replaying input.
The report records tool names, structured results and call times, without credentials.
Exit 0 means the selected checks passed; exit 4 means failure. For a double-click target,
add `--count 2`; it remains one bounded `vm_act` call. For a context menu use `--button right`.
The script does not reason visually, so a human or vision Agent must verify the application effect. `elapsed_ms`
includes transport/capture and other processing; it is not pure input or GUI frame latency.

## 7. Human takeover and conflicts

1. Click “接管” in the console. Keep that window focused and stay on that tab.
2. An independent MCP client can still observe, but `vm_act` must fail with `BUSY` without
   guest input. Another client can run the command below, or arrange a delayed launch;
   switching to a terminal would release control through loss of focus:

```bash
uv run python scripts/mcp_click_test.py \
  --root CONFIG_ROOT --vm-id VM_ID --x X --y Y --expect-error BUSY \
  --out .runtime/acceptance/takeover
```

3. Hold a key, drag and scroll on the console; verify guest response. An independent client
   should see the held key/button in `vm_status` while it is down.
4. Release with “交还 Agent” or Ctrl+Alt+G. Fresh observation permits Agent input again;
   an older frame must fail with `STALE_FRAME`. Blur, VM switches, leaving the console and
   display disconnect must release inputs too.
5. To test the three-second lease timeout, suspend heartbeats or deliberately disconnect the
   client. Idle traffic alone is not heartbeat loss. Check cleared held inputs and returned
   control. Unconfirmed release must continue blocking writes.

## 8. Further regression and exit

| Case | Action | Pass condition |
| --- | --- | --- |
| Old frame | Reuse the frame preceding a successful action | `STALE_FRAME`, no added effect |
| Duplicate | Repeat identical request_id and arguments | Same receipt, no duplicate click; changed arguments refused |
| Coordinates/zoom | Fit, native size and high DPI | Same target; MCP uses image pixels, human input uses the GUI transform |
| Fullscreen | Enter and leave console fullscreen | Display works; exit releases human control |
| Restart | Normal shutdown and startup | New instance, preserved managed disk/NVRAM, old input invalid |
| Exit confirmation | Close with an empty list, stopped machines, running machines or a disconnected backend | Always asks “退出 Palette？”; cancellation permits another close |
| Cancel exit | Close with a running VM, then cancel | Window and VM remain |
| Background | Choose “仅关闭窗口” | MCP observes; reopened Desktop shows the same instance |
| Complete exit | Choose “完全退出” | Graceful shutdown, then connected backend stop; window closes after original process exits; persistent data remains |
| Exit failure | Shutdown timeout, backend stop failure or disconnect | Window and error remain; retry or close only; never silently kills |
| Embedded exit | Choose complete exit while MCP stdin stays open | Embedded process exits, connections end, endpoints and lock are released; older embedded owners request an upgrade |
| Duplicate exit | Repeat close, clicks or Escape during exit | No duplicate submission or dialog dismissal |

WinPE's X: RAM disk is recreated at boot. Test persistence with a file on the managed disk,
after verifying the PE contains the storage-controller driver. ISO reset restores a blank
disk and discards managed-disk changes. Shut down normally before `daemon stop`; daemon
cleanup is not proof of guest shutdown.

## 9. Local ARM64 WinPE case

The supplied ISO is `CNBYDJ_PE_V4.0.0.ISO`. Its prepared local configuration is
`.runtime/winpe-acceptance/`, separate from ordinary VMs. The original ISO stays in place as
read-only media. The configuration uses aarch64 QEMU, HVF, `virt,gic-version=3,its=off`,
UEFI and `ramfb`, with 2 CPUs, 4 GiB memory, an 8 GiB blank disk and no network. See the
[QEMU Windows ARM display guidance](https://www.qemu.org/docs/master/system/whpx.html).

Reopen this test configuration:

```bash
PALETTE_ROOT="$PWD/.runtime/winpe-acceptance" npm --prefix desktop run preview
```

This ISO prompts “Press any key to boot from CD or DVD”; respond promptly. Missing the
prompt can leave a UEFI shell. Identify the installation-media filesystem from its Mapping
table; this local run showed `FS1:`. Enter `fs1:`, run `EFI\BOOT\BOOTAA64.EFI`, and promptly
press Enter again to confirm CD boot. Filesystem numbers can differ. Continue the Start-menu
test in step 5 only after the actual WinPE desktop appears.

### Local results, 2026-10-13

Host: macOS ARM64, QEMU 11.1.2, HVF. ISO SHA-256:
`11bc4c89e6529febc07beb60768c4bab2f3167c739ada9182547095363f2f61d`.
Local VM: `vm_489cdb67aca0`; instance: `inst_90f74625c80b`; display: 800 × 600.

| Check | Result and evidence |
| --- | --- |
| WinPE boot | Passed; confirmed CD boot and reached the real desktop |
| MCP initialization, status, image and keyboard | Passed through standard stdio proxies to the daemon |
| Start button | Receipt succeeded; immediate image did not verify a menu opening, so not independently passed as an application click |
| Double-click This PC | Passed; immediate image showed selection, later read-only evidence showed Explorer open |
| Right-click | Passed; later image showed the Explorer context menu |
| GUI synchronization | Passed; same instance, action sequence updated from 11 to 12, real display visible |
| Human takeover | Passed; observation remained available, click returned BUSY, release left empty and confirmed input ledgers |
| MCP proxy exit | Passed; repeated test-session exits left the VM and daemon running |
| Real restart, persistence and shutdown-all | Not executed; the test VM stays running for further manual checks |

Single full MCP click-call times were 693.15 ms for Start, 889.70 ms for double-click and
273.42 ms for right-click. They include transport, input and immediate capture, with one
sample each; they are neither percentiles nor GUI frame latency. Guest effects arrived
after some action receipts and were verified through later observation without replaying
uncertain actions. Local evidence is under `.runtime/winpe-acceptance/evidence/`, including
`gui-verification.json`, `gui-status.png`, and per-case JSON/PNG files; it is not committed.

Do not copy the HVF configuration to Windows/Linux. Select the platform's accelerator and
verify the PE boot path, display and storage drivers. A UEFI shell or missing boot device
requires checking a usable ARM64 EFI loader in the ISO; it is not a passed Agent desktop test.

## 10. Hardware settings acceptance

Use a disposable VM with a real guest. Record each item separately from fake-QEMU regressions:

1. Write a guest file, shut down, change CPU/RAM, and verify the resources in the guest after
   boot. Confirm running machines expose read-only settings and refuse operator edits.
2. Expand the managed disk, verify virtual capacity and the saved file, then expand the guest
   partition manually. Restart the backend and verify disk and UEFI persistence.
3. Change a supported default resolution and compare the requested setting with the actual
   guest geometry. For RAMFB confirm the unsupported control explains its limitation.
4. Replace/eject ISO media and test automatic, disk-first, and CD-first boot preferences.
5. Test offline/restricted/NAT independently. For an explicit forwarding rule, run a guest
   service, connect through localhost, and confirm the host listener is not publicly bound.
6. Enable sound with installed guest HDA drivers and play a test tone through the host.
   Verify no microphone capture; absent drivers or an unavailable audio backend are not passes.
7. Exercise unsaved changes, invalid values, reopened settings, old-backend notices, and
   screenshot/layout checks at the normal window sizes. A failed disk-integrity recovery must
   keep startup blocked and preserve its operation record.

### Local hardware checks, 2026-10-14

On macOS 27.0.1 / ARM64, QEMU 11.1.2 / HVF used a separate disposable VM and the supplied
WinPE ISO. Real `qemu-img` expanded the managed disk from 8 to 12 GiB; `qemu-io` verified a
4096-byte pattern at offset 1048576 survived, and UEFI variable bytes remained unchanged.
QMP confirmed two vCPUs. The live process arguments confirmed 1024 MiB RAM, a VirtIO NIC,
loopback-only forwarding, Intel HDA output, and CoreAudio initialization. A localhost
connection reached the forwarded listener; no guest service or outbound connectivity was
verified. VirtIO GPU received `xres=1920,yres=1080`, while the captured firmware framebuffer
remained 640×480. Guest partition expansion, guest resource checks, OS display-driver behavior,
and actual audio playback remain unverified. Evidence is local and ignored:
`.runtime/settings-acceptance/report.json` and `.runtime/settings-acceptance/display.png`.

## 11. Acceptance record

```text
Date / host OS / architecture:
Git SHA / Python / QEMU / accelerator:
ISO or image version / digest:
CONFIG_ROOT / runtime_root / daemon PID / generation:
VM_ID / instance_id / guest geometry:
MCP client / model / actual image received:
Click target / image coordinates / request_id / before-and-after action_seq:
Receipt / visible guest effect / GUI synchronization:
Takeover / BUSY / stale frame / input release:
Normal shutdown / proxy disconnect / GUI exit:
Timing definition / sample count / screenshot and report paths:
Passed, failed or unexecuted items / reasons:
```

Automated regression:

```bash
uv run pytest tests/integration/test_desktop.py tests/integration/test_vm_settings.py tests/unit/test_display.py
uv run python scripts/check_docs.py
npm --prefix desktop test
npm --prefix desktop run test:e2e
```

Electron tests use fake QEMU with real stdio MCP and local RFB. They exercise GUI creation,
MCP observation/click and GUI status updates. Passing them does not replace visual acceptance
on WinPE or another real guest.
