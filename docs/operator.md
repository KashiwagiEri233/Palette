# Operator guide

**English** | [简体中文](operator.zh-CN.md)

Palette runs a QEMU guest for an agent. This is what an operator does: check the host,
build a VM, watch it, and stop it when something goes wrong.

## Install

```bash
uv sync
uv run palette doctor
```

`doctor` checks the selected host/guest target, QEMU, firmware and image prerequisites.
Missing required components are blocking failures. A listed accelerator still needs a real
start to prove that the host can use it. The default selector is `auto`; see the
[platform matrix](platforms.md) for native macOS/HVF, Linux/KVM and Windows/WHPX profiles,
plus explicitly selected TCG.

Prerequisites on Windows:

1. QEMU for Windows, x86_64, ideally the version the profile pins.
2. Hardware virtualisation enabled in firmware, and the **Windows Hypervisor Platform**
   feature enabled (`WinHvPlatform.dll` present is necessary, not sufficient).
3. An ordinary user account. QEMU must not run elevated, and `doctor` warns if the shell is.
4. A guest template — see `guest-template.md`.

## The first thing to run on a new host

```bash
uv run python scripts/p0_probe.py
```

Start, screenshot, one click, screenshot again — the plan's P0 milestone, with the timings
the plan asks for (start to running, observe latency, click to post-action image). It
writes the frames to `.runtime/probe/` so you can look at them yourself, and it exits
non-zero with a specific reason when the host is not ready.

To check the transport rather than the VM:

```bash
uv run python scripts/mcp_stdio_smoke.py
```

That drives `palette serve` over real stdio exactly as a client would: it initialises a
session, lists the tools, calls one, and sends a malformed frame to confirm the session
survives it.

## Profiles

Everything trusted lives in `palette.toml` and `profiles/*.toml`. A profile carries the
QEMU path and version pin, the accelerator, the base image and its digest, the device set,
resource quotas, the network mode, capture and text limits, lifecycle permissions, the
retention policy and the guest-enhancement settings.

`qemu.arch` is the guest ISA, independent of the host executable. `tcg-x86_64` can run an
x86_64 guest on Apple Silicon without Rosetta; `tcg-aarch64` provides the inverse target.
These profiles use `cpu = "max"` and bounded software-emulation timeouts. Images must
already match the target ISA, BIOS/UEFI mode and devices; see [platforms](platforms.md).

Nothing there comes from the agent. Unknown keys are rejected, so a misspelled control
fails loudly rather than silently leaving a default in place:

```toml
[lifecycle]
allow_agent_resett = true    # refuses to load: did you mean allow_agent_reset?
```

One control cannot be relaxed, and the loader refuses a profile that tries:

* `allow_accelerator_fallback` — a silent fall back to TCG would change the isolation
  story the threat model depends on. Missing acceleration is a diagnosed failure.

Everything else is a switch, and the shipped `default.toml` takes the permissive side of
three of them: a NIC, the guest enhancement channel, and a caller's right to supply its own
image, CD-ROMs and QEMU arguments. `strict-example.toml` is the same machine with all of them
closed. Read `threat-model.md` before leaving the permissive set on: the last one removes the
host boundary, and no amount of argument filtering would put it back.

## Network modes

```toml
[network]
mode = "restricted-lab"          # or "offline", or "nat"
# mac = "52:54:00:12:34:56"
# hostfwd = ["tcp::2222-:22"]
```

| Mode | NIC | Guest's reach |
| --- | --- | --- |
| `offline` | none | nothing, and nothing reaches it |
| `restricted-lab` | user-mode, `restrict=on` | it cannot route past the sandbox |
| `nat` | user-mode | the same outbound reach the host has, with **no egress policy** |
| `internet-allowlist` | — | refused at start: an allowlist needs a gateway this build does not have |

`hostfwd` punches a hole from the host into the guest and is recorded when the VM starts,
because it undoes part of what `restrict=on` provides.

## Lifecycle

First run `uv run palette serve`, or use the instance already launched by the MCP client.
Keep its stdin open. In another terminal, use the same configuration root (`--root` if
specified). The CLI connects to that owner; it does not construct an independent manager.

```bash
uv run palette vm start  --vm-id desktop
uv run palette vm status --vm-id desktop
uv run palette vm stop   --vm-id desktop
uv run palette vm reset  --vm-id desktop
uv run palette vm list
```

A start can also carry host material, which is what the CLI flags below are for:

```bash
uv run palette vm start --vm-id scratch \
    --image D:/images/debian-testing.qcow2 \
    --iso  D:/templates/firmware.iso \
    --disk E:/data/scratch.qcow2 \
    --qemu-arg=-smp --qemu-arg=4
```

`--image` replaces the read-only backing store for this instance; the writable layer is still
created as qcow2 under `.runtime/`, including when `--image-format raw` selects a raw base.
Extra disks are inspected as raw or qcow2 and attached read-only using their actual format.
The manifest preserves the actual backing chain and attached files. `--qemu-arg` is appended last
and is exactly as powerful as it sounds — read `threat-model.md` first. Operators may always
use these; a caller acting as an agent needs the matching `allow_agent_*` switch.

* **start** creates a fresh overlay from the read-only base, starts QEMU hidden, negotiates
  QMP, resumes the paused guest and polls for a first valid framebuffer.
* **stop** asks the guest to shut down, waits the configured budget, then kills QEMU. A
  stop only reports success once the process is actually gone.
* **reset** is destructive and deliberate: revoke control, cancel the running action,
  release inputs, stop the process, keep the old layer per the retention policy, create a
  new overlay and start a new instance with a new identifier and boot epoch. The base image
  is never written to, and `qemu-img commit` is never used.
* **list** shows the on-disk instance directories, their manifests and their sizes.

State moves `STOPPED → STARTING → RUNNING → STOPPING → STOPPED`, with `PAUSED` and `FAILED`
as the plan defines. An illegal transition is reported rather than smoothed over.

Concurrent lifecycle commands serialize. Starting over a failed management channel first
reaps the old QEMU; an unconfirmed exit keeps the failed instance and its disk. Cancelling
startup rolls back its resources; cancelling a stop finishes stopping before returning.
The startup timeout covers overlay creation, launch, QMP negotiation and first-frame polling.
A guest's own `RESET` event advances `boot_epoch`, retires frames and reconnects guest tools
asynchronously. It does not discard the writable disk as a clean `vm reset` does.

## Emergency stop

```bash
uv run palette vm emergency-stop --vm-id desktop
uv run palette vm emergency-stop                  # every managed VM
```

Release every held input, cancel whatever is running, and kill QEMU now. Use it when an
agent is clicking at something it should not, when a key is stuck, or when you simply want
the desktop to stop changing.

## Taking control

```bash
uv run palette vm take-control --vm-id desktop --reason "human debugging"
```

Releases inputs, retires every frame the agent could still act on, and bumps
`controller_epoch` — so anything the agent derived from the previous state is refused with
`CONTROL_NOT_OWNED`. Reconnect a *view-only* console only through a mechanism that
provably cannot write; a plain writable VNC viewer would break the single-writer guarantee
and the service cannot detect it.

## Serving

```bash
uv run palette serve                 # MCP on stdio, for one client
uv run palette serve --profile other # a different default profile
```

Logs go to **stderr**. On stdio, stdout belongs to the MCP protocol, and a stray log line
there corrupts the session.

The operator control endpoint starts before MCP initialization, so a terminal can keep
`serve` running while another terminal uses the CLI. Closing the CLI leaves the VM alive;
EOF or exit of `serve` ends its ownership and stops its VMs. One owner is allowed per runtime
root. `vm list` reads manifests offline and does not require a live owner.
Windows console stdin retains line editing and Unicode input. Press Ctrl+Z followed by
Enter for EOF, or Ctrl+C to interrupt `serve`; both release the runtime and clean up its VMs.
Desktop complete exit can also stop a console owner without waiting for a line of input.

Operator control uses a Unix socket on POSIX and a local, current-user protected named pipe
on Windows. `.operator/endpoint.json` under the runtime root holds endpoint discovery,
service generation and an authentication token; `.operator/owner.lock` prevents duplicate
owners. Keep this directory private. It is not a remote API, and operator authority is
never supplied by an MCP tool argument.

## Where everything lives

```
<runtime_root>/                      # .runtime by default
├── vms/<vm_id>/<instance_id>/
│   ├── disk/overlay.qcow2           # the instance's writable layer
│   ├── frames/                      # scratch; emptied as each capture is delivered
│   ├── logs/
│   └── manifest.json                # what this instance is, and where it came from
└── audit/palette.jsonl              # bounded metadata: type, length, timing, outcome
```

Every deletion goes through a manifest and is confined to `runtime_root`. The service
refuses to touch a path outside it, and refuses to follow a junction or reparse point
inside it.

`overlay.qcow2.log.jsonl` and `.behavior.json` files appear next to the overlay when the
test doubles are used; they never appear in a real run.

## What the audit log holds

Action type, input length, timing, outcome and error code. It does **not** hold typed text,
launch arguments or screenshots, and it never holds host paths. A run that types a password
leaves no trace of the password — but remember that the screenshot itself went to the model,
and that is not something the log can undo.

## Retention

Both stop and reset apply retention. `keep_overlays = false` removes unused stopped layers;
otherwise `max_overlays` limits retained stopped layers besides active instances. A layer
that is still a backing dependency of another active or retained layer is protected, even
if this exceeds the count limit. Unconfirmed process exits and unknown historical manifests
are also preserved. Deletion happens only after process exit is confirmed. Screenshots
are not kept at all unless `capture.keep_frames` is set and `retention.persist_screenshots`
is enabled deliberately.

## Recovery

| Symptom | Meaning | Action |
| --- | --- | --- |
| `state: FAILED` | QEMU exited, or the management channel dropped | `vm_status` for the reason, then `vm reset` |
| `QMP_UNAVAILABLE` | the private channel is gone | the instance is not usable; `vm stop --force` and start again |
| `screen_ready: false` | no valid framebuffer | the guest may still be booting; check `vm_stop`/`vm_start` if it persists |
| `desktop_ready: null` | never verified | expected: the service does not infer readiness. Look at a screenshot |
| A stuck key | a press was not released | `vm_release_inputs`, or `emergency-stop` if the channel is unhealthy |
| `PRECONDITION_FAILED` from a lifecycle CLI | no running owner for this runtime | start `palette serve` with the same `--root`; `vm list` can inspect retained files offline |
| `STATE_CONFLICT` while starting `serve` | another service owns the runtime | use that service or select a distinct runtime root |
| `ACTION_OUTCOME_UNKNOWN` from the CLI | the connection failed after sending the command | inspect status before deciding what to do; the CLI never retries automatically |
| The service died | its private QMP pipes cannot be adopted | Windows Job Object or POSIX supervisor cleans up; restart `serve` and create a new instance |
| `ACCELERATION_UNAVAILABLE` | wrong ISA/host or unavailable hypervisor | fix the native prerequisites, or explicitly select a matching `tcg-*` target |
| AArch64 firmware is missing | no matching code/vars pair was found | install EDK2/AAVMF or set both firmware paths; see [platforms](platforms.md) |

## Testing

```bash
uv run pytest                      # unit, QMP, guest-tool and integration tests
uv run pytest tests/e2e            # skipped unless a real QEMU and image are configured
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run python scripts/check_docs.py
```

The integration suite starts a real child process that speaks QMP over real pipes, so it
covers framing, teardown and lifecycle behaviour without needing a hypervisor. The docs check
enforces the two-language pairing and that every relative link resolves.

The workflow in `.github/workflows/ci.yml` defines the current OS/Python matrix and dispatch
conditions. Platform-specific branches need their own tests: Windows Job Objects/named pipes
and POSIX supervisors/Unix sockets are different implementations. Type-check all three targets.

To opt into E2E, set `PALETTE_E2E=1` and `PALETTE_E2E_PROFILE` to the intended configured
profile, such as `tcg-x86_64`. `PALETTE_E2E_QEMU`, `PALETTE_E2E_IMAGE` and
`PALETTE_E2E_ACCEL` optionally override its executable, base image and accelerator. Without
`PALETTE_E2E_PROFILE`, the QEMU/image overrides use the configured default profile's guest
architecture; selecting a different binary alone does not change the target ISA.

**This change did not run physical acceptance.** E2E tests require an explicitly configured
QEMU and matching guest image; otherwise they skip. Fake-process CI results do not establish
hypervisor availability, cross-architecture boot compatibility or real desktop latency.
