# Operator guide

**English** | [简体中文](operator.zh-CN.md)

Palette runs a QEMU guest for an agent. This is what an operator does: check the host,
build a VM, watch it, and stop it when something goes wrong.

## Install

```bash
uv sync
uv run palette doctor
```

`doctor` is the gate. It reports a hard failure for anything it cannot verify — most
commonly a missing QEMU binary. It never offers a degraded mode as an alternative:

```
palette 0.1.0 doctor
  host      : Windows-11-10.0.26200-SP0 (AMD64)
  python    : 3.12.13
  profile   : default (visual_only)
  checks    :
    [FAIL] qemu_binary: not found on PATH or in the search paths
  result    : NOT READY
```

Prerequisites on Windows:

1. QEMU for Windows, x86_64, ideally the version the profile pins.
2. Hardware virtualisation enabled in firmware, and the **Windows Hypervisor Platform**
   feature enabled (`WinHvPlatform.dll` present is necessary, not sufficient).
3. An ordinary user account. QEMU must not run elevated, and `doctor` warns if the shell is.
4. A guest template — see `guest-template.md`.

## The first thing to run on a new host

```bash
uv run python scripts/p0_probe.py --profile default
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

Nothing there comes from the agent. Unknown keys are rejected, so a misspelled control
fails loudly rather than silently leaving a default in place:

```toml
[lifecycle]
allow_agent_resett = true    # refuses to load: did you mean allow_agent_reset?
```

Two controls cannot be relaxed, and the loader refuses a profile that tries:

* `allow_accelerator_fallback` — a silent fall back to TCG would change the isolation
  story the threat model depends on. Missing acceleration is a diagnosed failure.
* extra QEMU arguments that add a monitor or display channel — a second, unauthenticated
  input path would break the single-writer assumption.

## Lifecycle

```bash
uv run palette vm start  --vm-id desktop --profile default
uv run palette vm status --vm-id desktop
uv run palette vm stop   --vm-id desktop
uv run palette vm reset  --vm-id desktop
uv run palette vm list
```

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

Stopped instances are kept so their results can be retrieved, up to `max_overlays` besides
the running one. Lower it if disk growth matters more than keeping recent work. Screenshots
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
| The service died | orphaned QEMU | on Windows the job object kills it; otherwise `palette vm emergency-stop` |

## Testing

```bash
uv run pytest                      # unit, QMP, guest-tool and integration tests
uv run pytest tests/e2e            # skipped unless a real QEMU and image are configured
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

The integration suite starts a real child process that speaks QMP over real pipes, so it
covers framing, teardown and lifecycle behaviour without needing a hypervisor.
