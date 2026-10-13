# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Palette gives a visual agent an observable, operable, resettable QEMU guest desktop over MCP.
Most of what looks arbitrary here is deliberate: it encodes a safety property, and several files
usually depend on it together. The invariants section below is the compressed form of those
rules; `docs/` explains the same design from the outside.

There are two paths, and only one of them is required to work:

```
baseline     Agent → MCP server → controlled QMP → standard HID devices / framebuffer
enhancement  Agent → same MCP server → Guest Tool Bridge → in-guest tools (requires a guest service)
```

The baseline must remain independently sufficient. The enhancement path shares the policy,
scheduler and audit, but nothing visual may come to depend on it, and a failed enhancement call
must never be silently downgraded into keystrokes or a retry. This build ships the enhancement
contract, a disabled backend, a mock and a bridge — not a guest-side service.

Defaults that constrain feature work: a sandboxed NIC (`restricted-lab`), the guest enhancement
channel enabled, a caller permitted to supply its own base image, CD-ROMs and raw QEMU
arguments — and, still closed, an enforced egress allowlist, TCG fallback, persistent
screenshots, and the destructive lifecycle operations. `profiles/strict-example.toml` closes
every switch and is the same machine otherwise.

Two documentation conventions are enforced by hand, not by tooling:

* **`docs/` is bilingual.** Every page exists as `X.md` and `X.zh-CN.md`. A change to one is
  incomplete until the other is updated; both open with a language switcher line.
* **Never claim more than was verified.** The Windows/WHPX, macOS/HVF, Linux/KVM and explicit
  cross-architecture TCG matrix has not undergone physical guest acceptance in this change.
  `doctor` checks prerequisites, and `tests/e2e` skips without an explicit emulator/image.
  Fake-process tests do not prove guest compatibility, capture latency or click accuracy.
  Keep `docs/benchmarks.md` free of claimed results until a real measurement exists.

## Commands

```bash
uv sync                                     # install; the venv lives in .venv
uv run palette doctor                       # host/QEMU/profile probe; exits 3 when not ready
uv run palette serve                        # MCP server on stdio (logs go to stderr)
uv run palette vm start|stop|reset|status   # second terminal; contacts the running serve
uv run palette vm emergency-stop|take-control|list
uv run palette config show

uv run pytest                               # full suite (~3 min; the integration tests spawn processes)
uv run pytest tests/unit                    # fast subset
uv run pytest tests/unit/test_controller.py::TestFrameFencing -q
uv run pytest tests/unit/test_controller.py -q -k "resize"
uv run pytest tests/e2e -v                  # skipped unless PALETTE_E2E* env vars are set

uv run ruff check . && uv run ruff format . # lint and format; both are enforced
uv run mypy                                 # strict, covers src/palette and scripts
uv run python scripts/check_docs.py         # bilingual pairing, links, fences, UTF-8
uv run mypy --platform linux                # see the platform note below
uv run mypy --platform win32
uv run mypy --platform darwin
```

`.github/workflows/ci.yml` is the source of truth for the OS/Python matrix and dispatch
conditions. The suite, stdio smoke and doctor smoke exercise platform-specific implementations:
Windows Job Objects/named pipes and POSIX supervisors/Unix sockets need their respective hosts.

Whichever jobs run, the physical-acceptance tests need a real QEMU and a guest image, so they
**skip**; that is not coverage of them, and nothing in the workflow or the docs should imply it
is.

**Platform branches are type-checked too.** mypy narrows `sys.platform` to the machine it runs
on, so a Windows-only branch is judged against Linux's stubs when checkers run there — the
first CI run failed with `ctypes.WinDLL` "missing" on Ubuntu, in Windows code that never
executes there, and the mirror case (`os.geteuid`) is just as real. Guard such branches with a
plain boolean (`IS_WINDOWS = sys.platform == "win32"`) and reach single-platform attributes
through `getattr`, then check both directions locally. The lint job runs on both platforms for
the same reason.

`scripts/p0_probe.py` runs the plan's P0 milestone (start → screenshot → click → screenshot)
against whatever profile you point it at, and prints the timings. `scripts/mcp_stdio_smoke.py`
drives `palette serve` over real stdio the way a client would.

Keep CLI and script output **ASCII-only** so supported Windows GBK consoles can print it.

## Architecture

The request path is strictly layered, and each layer is written not to trust the one above it:

```
MCP client → server.py → service.py → policy.py → input/controller.py → qmp/client.py → QEMU
                            ↓                          ↓
                     vm/manager.py            capture/qmp.py, input/{coordinates,keyboard}.py
```

* `server.py` — MCP tool contracts only. Schemas, image blocks, structured results.
* `service.py` — the only component that knows both the tool surface and the machinery.
  Authorization, dispatch, audit and lifecycle meet here.
* `policy.py` — path confinement, lifecycle authority, and the capability registry.
* `input/controller.py` — the bounded action transaction; the single writer per VM.
* `qmp/client.py` — the private management channel. Knows nothing about JSON-RPC or tools.

`CaptureBackend`, `ManagedProcess`, `ProcessGuard`, `GuestToolBackend` and `GuestToolTransport`
are the extension seams. Windows uses a direct managed process and Job Object; POSIX uses a
supervisor with the same process/guard interface, while QMP still uses direct private pipes.

`host.default_profile = "auto"` selects `default` on Windows x86_64, `macos-x86_64` or
`macos-aarch64` on macOS, and `linux-x86_64` or `linux-aarch64` on Linux. `qemu.arch` is the
guest ISA. `tcg-x86_64` and `tcg-aarch64` are explicit software-emulation choices with
`cpu=max`, never automatic fallbacks; Apple Silicon does not need Rosetta for an x86 guest.
Firmware, boot mode and guest drivers must match the guest target. See `docs/platforms.md`.

`docs/` explains the design from the outside; `.references/qemu` is a pinned QEMU checkout for
research. Verify qcodes, QAPI unions and event shapes against it rather than from memory — that
is how the `KeyValue` wire form (`{"type":"qcode","data":"a"}`) and the `InputButton` names
were settled. `.references/` is research only, never project source.

## Invariants that are easy to break

These span several files and are not obvious from any single one.

**One writer per VM.** Every desktop-affecting operation — QMP input *and* guest-tool text
input and app launches — goes through `ActionController`'s job queue (`_submit` → `_run_worker`).
Never call `qmp.execute("input-send-event", ...)` from anywhere else. Observes and waits are
queued too, so nothing observes a half-finished action.

**Frame fencing.** A frame is refused unless it matches the current instance, boot epoch,
controller epoch, display epoch, *and* `action_seq`, and is within `frame_ttl_seconds`, and its
`native_size` still matches. QMP has no resize notification, so `_ensure_size_is_current()`
re-captures before a coordinate action at most once per `capture.size_check_interval_seconds`.

**De-duplication runs before the stale-frame check.** A retry of a successful action must return
the stored receipt, not `STALE_FRAME` — the frame is stale precisely because the action worked.
`RequestDeduplicator` also collapses concurrent identical requests onto one future.

**Never replay an action.** After `TIMEOUT` or `QMP_UNAVAILABLE` following partial input the
receipt says `partial` or `unknown`, and nothing retries. If the input went out but the
follow-up capture failed, the receipt is `CAPTURE_FAILED_AFTER_INPUT` with
`effect_state=submitted` and no frame — report the input, never re-send it.

**Ledger asymmetry.** Presses are recorded *before* the send (an unknown press is safer than a
missed one); releases are recorded only *after* the send succeeds (a failed release leaves the
key held). Cleanup releases only what the ledger knows about.

**Frame retirement carries a reason.** `FrameRegistry` remembers retired ids briefly, so acting
on a frame killed by a resize yields `STALE_FRAME` with a reason, while an id that was never
issued yields `FRAME_NOT_FOUND`. Both are refusals, but only one means "observe again and retry".

**`input-send-event`'s `device` argument is the *display* id** (`video0`), never the keyboard's
or pointer's. `screendump` uses the same display id.

**Result models validate themselves.** Each tool's published output schema is the domain model
(`Annotated[CallToolResult, Model]` — that pattern is what publishes `outputSchema` *and*
validates `structuredContent`; plain `-> CallToolResult` publishes the wrong schema). Success
models require their payload, failure bodies require an `ErrorPayload`. `Frame.monotonic_stamp`
is excluded from the wire and defaults to `0.0` so a dumped-and-reparsed frame still validates.

**Process spawning stays in `vm/launcher.py`.** Helpers, the POSIX supervisor and its QEMU
child use the same environment whitelist and appropriate process/session flags. Never use
`preexec_fn`. A supervisor's liveness reader must not leak into QEMU. POSIX memory limits are
250 ms RSS sampling, not a Job Object equivalent hard cap; report the enforcement method.

**One runtime has one service owner.** Lifecycle CLI commands use authenticated local IPC to
the existing `serve`; only `vm list` reads manifests offline. Do not reintroduce a temporary
`PaletteService` into CLI lifecycle commands or automatically replay an uncertain IPC request.

**Cancellation does not abandon owned resources.** Startup rolls back partial instances;
stop/reset/shutdown finish their cleanup before propagating cancellation. A failed instance
is not replaced until its old process exits. Identity checks and lookup happen under the
lifecycle lock, so authorization for one instance cannot operate on its replacement.

**Deletion requires a manifest.** The manifest is written *before* the overlay is created, so the
failure path can clean up through the same manifest-verified route. Every managed path goes
through `PathGuard`, which checks lexically *and* refuses to follow a junction or reparse point.
Manifest IDs must match the directory; updates preserve the actual base, formats, backing
chain and attached volumes. Stop and reset share retention. Exit must be confirmed before
deletion, and dependency protection takes priority over the stopped-layer count limit.

**A guest RESET is a new boot boundary.** Advance `boot_epoch` and invalidate controller work
and frames synchronously. Replace the guest-tool session and reconnect it asynchronously;
the visual baseline must remain usable. A guest reset does not discard the writable disk.

**Base format is not overlay format.** Independently inspect the actual base format before
creating an overlay, then use `-f qcow2 -F <actual declared base format>`. The guest's writable
block node is always qcow2. Extra disks use their inspected format, not an assumed raw driver.

**Profiles are frozen and `extra="forbid"`.** A misspelled safety switch must fail loudly.
One control deliberately cannot be relaxed: `allow_accelerator_fallback`. Everything else —
including arguments that add a monitor or display channel — is a switch, and the loader takes
an operator's arguments at face value.

**Caller-supplied host material is its own axis.** `allow_agent_images`, `allow_agent_iso` and
`allow_agent_qemu_args` are authorised, validated and audited one at a time via
`Policy.require_host_material`; a refusal names the switch. Paths go through
`vm.images.validate_caller_path` (absolute, exists, a regular file — **not** `PathGuard`, since
these are deliberately outside the runtime root) and arguments through
`service._validate_qemu_args`, which bounds count and length but deliberately does not filter
content. A caller-supplied image becomes the read-only backing store for an overlay that is
still ours, still per-instance, and still discarded by reset; the manifest records it. Read
`docs/threat-model.md` before touching any of this: with `allow_agent_qemu_args` on, every
other invariant in this section is advisory, and the service says so rather than pretending a
blocklist would help.

**Guest tools are registered only if a profile enables them** — and every call still re-checks
the profile grant and the live capability handshake. Reported capabilities are the intersection
of what the profile grants and what the guest advertises; advertising is not permission.

**The enhancement channel is never a prerequisite.** A guest service that is offline, slow or
incompatible must degrade to "unavailable", never to silent key simulation and never to an error
that stops the visual tools working. A guest call that changes the desktop advances `action_seq`
through the same path as a click, so it fences earlier frames identically.

**The audit trail records shape, not content.** Action type, length, timing, outcome. `AuditLog`
scrubs keys like `text`/`path`/`token` before writing. Never widen that.

## Extending

* **New action type** — add it to the closed union in `models.py` (`_ActionBase` subclass, bounds
  on every field), plan its event batches in `ActionController._plan_batches`, and cover it in
  `tests/unit/test_controller.py`. The union's discriminator and forbidding of unknown fields are
  load-bearing; there is deliberately no raw press/release surface.
* **New capability** — register it in `policy.REGISTERED_CAPABILITIES`, add a schema, and gate it.
  An unregistered capability is reported as missing, not as disabled, so nobody thinks a config
  change can turn it on.
* **New capture backend** — implement `capture.base.CaptureBackend`; the controller only needs
  `capture(display_id, head, sink) -> CapturedImage`.
* **Real guest tool transport** — implement `GuestToolTransport` and replace
  `VirtioSerialGuestToolTransport`. `guest/agent/README.md` is the contract; `MemoryGuestToolTransport`
  already models a channel that stalls, disconnects and reorders.

## Testing

`tests/` is organised by what a failure there would mean, and the split is deliberate:

* `tests/unit` — pure logic against in-process doubles (`tests/support/doubles.py`).
* `tests/qmp` — the protocol layer against a scripted in-process QMP peer (`fake_qmp_server.py`).
* `tests/guest_tools` — the enhancement contract against a mock guest service.
* `tests/integration` — **a real child process** speaking real QMP over real pipes. `fake_qemu.py`
  is the emulator and `fake_qemu_img.py` is qemu-img; both are reached through a profile whose
  `qemu.binary` is `sys.executable` and whose `argv_prefix` / `qemu_img_prefix` are `-I <script>`.
  This is what makes the lifecycle, teardown and 20-cycle tests real rather than mocked.
* `tests/e2e` — physical acceptance, gated on `PALETTE_E2E`, `PALETTE_E2E_QEMU`,
  `PALETTE_E2E_IMAGE`.

Useful fixtures in `tests/conftest.py`: `profile`, `config`, `service` (a started
`PaletteService`), `running_vm` (a booted fake VM), `base_image`, `runtime_root`, plus the
helpers `read_command_log` and `write_behavior` to inspect and steer the running fake QEMU.
`harness_factory` (an `ActionController` over in-process doubles) lives in
`tests/unit/test_controller.py`.

A test that cannot distinguish its subject from a stub is not worth writing — that is why the
lifecycle tests use a subprocess and why the capture tests assert *which* QMP error triggers the
PPM fallback.
