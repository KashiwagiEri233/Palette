# Host Operator Runbook

**English** | [简体中文](operator.zh-CN.md)

**Palette** runs supervised QEMU virtual machines for visual agents and Palette Desktop. As the host operator, your responsibilities are probing the host environment, maintaining trusted profiles, running the shared backend (`daemon` or `serve`), managing VM lifecycle, and taking over or triggering an emergency stop when needed.

## 1. Installation & Host Verification

```bash
uv sync --locked
uv run palette doctor
```

`palette doctor` probes host/guest target compatibility, the QEMU binary and version pin, required device models (`VGA`/`virtio-gpu-pci`, `qemu-xhci`, `usb-kbd`, `usb-tablet`, `virtio-blk-pci`), hardware accelerators, UEFI firmware, the base image, `.runtime/` write permissions, and `qemu-img`:

* Any missing blocking prerequisite exits with code `3` (`EXIT_PRECONDITION`); Palette never hides missing hardware acceleration behind a silent TCG fallback.
* Pass `--json` for machine-readable output, or `--hash-base` to compute and verify the full SHA-256 digest of the base image.
* The default profile selector is `auto`. See the [platform guide](platforms.md) for native Windows (`WHPX`), macOS (`HVF`), Linux (`KVM`), and explicit `TCG` profiles.

### Platform Prerequisites

1. **Run as a Normal Non-Admin User**: Never run QEMU elevated (Administrator on Windows or `root` on POSIX). `palette doctor` emits a `WARN` check if invoked from an elevated shell.
2. **Windows (`WHPX`)**: Install x86_64 QEMU for Windows, enable hardware virtualization in BIOS/UEFI, and enable the **Windows Hypervisor Platform** feature (`WinHvPlatform.dll` presence is checked by `doctor`; actual hypervisor launch is proven by starting a VM).
3. **macOS (`HVF`)**: Install `qemu` via Homebrew (`brew install qemu`). Apple Silicon defaults to `macos-aarch64` (with EDK2/AAVMF UEFI firmware); Intel Macs default to `macos-x86_64`.
4. **Linux (`KVM`)**: Install the target QEMU system emulator and `qemu-img`, and grant your user read/write access to `/dev/kvm`.

## 2. First-Run Smoke Tests & Hardware Probe

On a newly configured host, run these two verification scripts first:

```bash
# 1. Verify MCP stdio framing, tool listing, and malformed-frame resilience
uv run python scripts/mcp_stdio_smoke.py

# 2. Run the P0 hardware probe: cold boot -> capture -> click -> post-action capture with timing
uv run python scripts/p0_probe.py
```

`p0_probe.py` writes captured frames to `.runtime/probe/` for visual inspection and exits non-zero with a specific error code if the host or image is not ready.

## 3. Trusted Configuration (`palette.toml` & `profiles/*.toml`)

All trusted settings live in `palette.toml` and `profiles/*.toml`. Inspect the resolved configuration with:

```bash
uv run palette config show
```

* **Strict Schema Validation**: Nothing in configuration is ever derived from agent input. Unknown keys are rejected at load time so typos fail loudly instead of silently leaving a default in place:
  ```toml
  [lifecycle]
  allow_agent_resett = true    # Rejected at load: did you mean allow_agent_reset?
  ```
* **No Silent Accelerator Fallback**: The loader rejects `allow_accelerator_fallback`. Missing hardware acceleration is a diagnosed failure because software emulation (`tcg`) changes the guest isolation guarantees.
* **Permissive Default vs. Strict Posture**:
  * `profiles/default.toml` enables a sandboxed NIC (`restricted-lab`), the guest enhancement channel, and caller-supplied host material switches (`allow_agent_images`, `allow_agent_iso`, `allow_agent_qemu_args`).
  * `profiles/strict-example.toml` defines the exact same machine with all caller switches closed and `offline` networking. **Read the [threat model](threat-model.md) before operating with open switches.**

### Network Modes (`[network]`)

```toml
[network]
mode = "restricted-lab"          # "offline" | "restricted-lab" | "nat"
# mac = "52:54:00:12:34:56"
# hostfwd = ["tcp:127.0.0.1:2222-:22"]
```

| Mode | Virtual NIC | Guest Reachability |
| --- | --- | --- |
| `offline` | None | Completely air-gapped; nothing reaches the guest and the guest reaches nothing |
| `restricted-lab` | User-mode (`restrict=on`) | Confined to QEMU's internal slirp sandbox; cannot route out to the host or LAN/internet |
| `nat` | User-mode | Full outbound reach equivalent to the host process, **with no egress filtering** |
| `internet-allowlist` | — | Refused at start: user-mode NAT cannot enforce an outbound allowlist |

> `hostfwd` punches an inbound port forward from the host into the guest and is recorded at VM start (port forwards created in Palette Desktop bind strictly to `127.0.0.1`).

## 4. Service Modes: Shared `daemon` vs. `serve`

Each `runtime_root` permits at most one active `RuntimeOwner` at a time (enforced via `.operator/owner.lock`).

### Mode A: Shared Background Daemon (`palette daemon`, Recommended)

Use `palette daemon` when sharing VMs across **Palette Desktop**, the **Operator CLI**, and **one or more MCP clients**:

```bash
uv run palette daemon start           # spawn background owner (idempotent if already running)
uv run palette daemon status          # inspect owner PID, kind, version, and generation
uv run palette daemon stop            # stop daemon and clean up all managed QEMU processes
uv run palette daemon run             # run owner in the foreground
```

While the daemon is running:

* `uv run palette serve` automatically discovers the daemon and runs as a lightweight `stdio` proxy; disconnecting the MCP client leaves the daemon and VMs running.
* `uv run palette serve --connect-only` requires an existing owner and fails with `PRECONDITION_FAILED` if none is running.

### Mode B: Standalone MCP Session (`palette serve --standalone`)

```bash
uv run palette serve --standalone     # own the runtime until stdio closes, then stop all VMs
```

* All logs go to **`stderr`** so `stdout` remains reserved exclusively for MCP JSON-RPC messages.
* The local operator IPC endpoint (POSIX Unix Socket / Windows current-user Named Pipe) starts before MCP initialization, allowing `palette vm ...` CLI commands from another terminal sharing the same `--root`.

## 5. VM Lifecycle & Emergency Controls

With a `daemon` or `serve` owner running, run operator lifecycle commands from any terminal (executed with the trusted `operator` role):

```bash
uv run palette vm start  --vm-id desktop
uv run palette vm status --vm-id desktop
uv run palette vm stop   --vm-id desktop
uv run palette vm stop   --vm-id desktop --force
uv run palette vm reset  --vm-id desktop
uv run palette vm list
```

You can also pass explicit host material on startup:

```bash
uv run palette vm start --vm-id scratch     --image D:/images/debian-testing.qcow2     --iso   D:/templates/firmware.iso     --disk  E:/data/scratch.qcow2     --qemu-arg=-smp --qemu-arg=4
```

* **`start`**: Creates a fresh writable `overlay.qcow2` backed by the read-only base image (or uses the persistent writable disk for Desktop-defined VMs), launches QEMU headless, completes the private QMP handshake, resumes the guest, and polls for the first valid framebuffer.
* **`stop`**: Sends an ACPI shutdown request, waits up to the configured budget, and terminates QEMU; reports success only after the process exits.
* **`reset`**: Revokes control leases, cancels in-flight actions, releases held inputs, stops QEMU, applies overlay retention, and boots a brand-new `overlay.qcow2` with a new `instance_id` and incremented `boot_epoch`. The base image is never modified and `qemu-img commit` is never used.
* **`list`**: Reads instance manifests and disk usage directly from `.runtime/` offline (no running owner required).

### Emergency Stop

```bash
uv run palette vm emergency-stop --vm-id desktop
uv run palette vm emergency-stop                  # omit --vm-id to stop every managed VM
```

Immediately releases all held inputs, cancels queued actions, and kills the QEMU process. Use this whenever an agent is clicking the wrong target, a key is stuck, or you need the desktop frozen immediately.

### Operator Control Takeover

```bash
uv run palette vm take-control --vm-id desktop --reason "human debugging"
```

Releases held inputs, increments `controller_epoch`, and retires every active `frame_id` so stale agent actions are rejected with `CONTROL_NOT_OWNED`. For live interactive takeover with mouse and keyboard, use the [Palette Desktop console](desktop.md).

## 6. Runtime Directory Layout, Auditing & Retention

### `.runtime/` Layout

```
<runtime_root>/                      # defaults to .runtime/ (enforced by PathGuard)
├── .operator/
│   ├── endpoint.json                # local IPC discovery, service generation, and auth token
│   └── owner.lock                   # single-owner lockfile
├── definitions/                     # persistent VM definitions and disks created by Palette Desktop
│   └── <vm_id>/
├── vms/<vm_id>/<instance_id>/       # per-instance runtime directory
│   ├── disk/overlay.qcow2           # writable qcow2 overlay
│   ├── frames/                      # transient screenshot staging (wiped after delivery)
│   ├── logs/                        # instance logs
│   └── manifest.json                # instance provenance, backing chain, and attached media
└── audit/
    └── palette.jsonl                # rotating metadata-only audit log
```

### Audit Log Privacy

`audit/palette.jsonl` records only action type, input length, timing, outcome, and error code. It **never** logs typed text, launch arguments, persistent screenshots, or host paths.

### Retention Policy (`[retention]`)

* Both `stop` and `reset` enforce retention. `keep_overlays = false` deletes stopped overlays once process exit is confirmed; `keep_overlays = true` retains the most recent `max_overlays` stopped overlays.
* Overlays still referenced as a backing file by another active or retained disk, or whose QEMU process exit is unconfirmed, are protected from deletion.
* Screenshots are deleted immediately after delivery unless both `capture.keep_frames` and `retention.persist_screenshots` are explicitly enabled.

## 7. Fault Recovery Playbook

| Symptom / Error Code | Meaning | Recovery Action |
| --- | --- | --- |
| `state: FAILED` | QEMU exited unexpectedly or the QMP pipe broke | Inspect with `palette vm status`, then run `palette vm reset` |
| `QMP_UNAVAILABLE` | Private QMP pipe is closed | Run `palette vm stop --force` followed by `palette vm start` |
| `screen_ready: false` | No valid framebuffer captured yet | Guest may still be booting; if persistent, check firmware/display config and restart |
| `desktop_ready: null` | In-guest service has not verified desktop login | Expected for visual baseline: inspect a screenshot via `vm_observe` or Palette Desktop |
| Stuck key or button | Input chord interrupted before release | Call `vm_release_inputs`, or `palette vm emergency-stop` if QMP is unhealthy |
| CLI returns `PRECONDITION_FAILED` | No running owner for this `runtime_root` | Start `uv run palette daemon start` with the same `--root` (`vm list` works offline) |
| `serve` returns `STATE_CONFLICT` | Another standalone owner holds `runtime_root` | Connect to the existing owner (omit `--standalone`) or use a separate `runtime_root` |
| CLI returns `ACTION_OUTCOME_UNKNOWN` | Local IPC disconnected after sending command | Check `palette vm status` before deciding whether to retry |
| `ACCELERATION_UNAVAILABLE` | Host/guest ISA mismatch or hypervisor disabled | Enable host virtualization (WHPX/HVF/KVM) or explicitly select `--profile tcg-x86_64` / `tcg-aarch64` |
| Missing AArch64 firmware | EDK2/AAVMF code/vars not auto-discovered | Install QEMU EFI firmware or set `qemu.firmware.code` and `vars` in the profile (see [platforms](platforms.md)) |
