# Known limitations

**English** | [简体中文](known-limitations.zh-CN.md)

An honest list. Everything here is a deliberate scope boundary or a real constraint, not an
oversight, and none of it is hidden behind a friendlier message at runtime.

## Environment

* **The expanded platform matrix has not had physical acceptance in this change.** Fake
  QMP and subprocess tests do not prove real guest boot, capture latency or click accuracy.
  E2E skips unless a real QEMU and matching image are configured.
* **Platform support is explicit.** Native Windows x86_64/WHPX, macOS Intel/Apple Silicon
  with HVF, and Linux x86_64/aarch64 with KVM have profiles and process protection. Explicit
  `tcg-x86_64`/`tcg-aarch64` profiles provide software emulation; no fallback occurs. See
  [platforms](platforms.md) for prerequisites and the unverified physical matrix.
* **POSIX memory limits are sampled.** RSS is checked every 250 ms; temporary overshoot is
  possible. This is not a Windows Job Object equivalent hard cap. The supervisor controls
  the QEMU process group, not processes deliberately escaping it with a new session.
* **No guest image ships.** The base image is an operator artefact with a recorded origin
  and digest.

## Capability

* **Unicode text is not supported in visual mode.** Strict keyboard mode covers US-layout
  printable ASCII. Chinese, emoji and multi-line text need the guest enhancement channel,
  which this build defines but does not implement.
* **`type_text` is slow by design.** One input message per key phase, rate limited, so a
  256-character string is hundreds of round trips. It does not share the click latency
  budget.
* **No cross-VM scheduling.** A service can own several VMs, each with its own action queue,
  but does not balance their resources or schedule work across them.
* **Vertical scrolling only.** Horizontal scrolling is expressible in QAPI and deliberately
  not exposed: it is untested, and offering it "because the enum exists" would be a claim
  without evidence.
* **No touch, gesture or pressure input.** No multi-touch, no relative pointer lock.
* **`vm_wait` change and stability are pixel heuristics.** A blinking cursor, a clock or an
  animation defeats both. Neither means "the application is idle".
* **No clipboard.** Not the host's, and not by default the guest's either.
* **No audio, no video, no USB passthrough, no shared folders.**

## Correctness boundaries

* **Nothing the service does survives an open `allow_agent_qemu_args`.** That is not a bug to
  be fixed later; it is what the switch means. See `threat-model.md`.
* **Pixel comparison cannot detect semantic change.** Two identical-looking screenshots can
  hide a moved button. Payment, deletion and submission still need the caller's own
  confirmation policy.
* **Exactly-once is in-process only.** De-duplication is bounded memory inside one service
  lifetime. Across a crash, the honest answer is `unknown`, and the service says so instead
  of replaying.
* **A private anonymous pipe cannot be re-adopted.** When the service dies, the Windows Job
  Object or POSIX supervisor cleans up QEMU. A new service must start a new instance.
* **Frames are not archived.** Only a 64×64 thumbnail per frame is retained, and only to
  answer "has the screen changed?".
* **The pointer's real position is unknown.** `cursor_position_source` is `not_observed`
  unless the service moved it, and it never claims to have observed the cursor.
* **`screendump` is not an atomic desktop snapshot.** Updates during the write can produce a
  torn image. It is not presented as a transactional view.

## Operations

* **No remote MCP transport.** MCP uses stdio; the authenticated local operator socket/pipe
  serves the CLI only. Streamable HTTP would need TLS, authentication,
  per-VM authorization, strict `Origin`/`Host` validation, limits and audit.
* **No multi-tenant isolation.** One operator, one host user. See the threat model for what
  that does and does not buy.
* **No scheduling platform.** Clients address VMs by `vm_id`; coordinating tasks across them
  remains the caller's responsibility.
* **Guest enhancement tools are enabled by default and still unimplemented.** The contract,
  the policy gates, the bridge and the mock exist; the guest-side service and its transport do
  not. Enabling the channel costs nothing and delivers nothing until such a service answers
  the handshake, so `vm_status` reports transport and service separately and every call
  against a service-less guest fails as unavailable.
* **An enforced egress allowlist is not implemented.** `offline`, `restricted-lab` and `nat`
  work; `internet-allowlist` is refused at start rather than approximated with user-mode NAT,
  which is not an allowlist.
* **No IPv6, DNS-rebinding or redirect handling.** The `nat` mode gives the guest the host's
  outbound reach as-is, including whatever the host can resolve and reach.

## What is implemented and tested

So the list above is not read as "nothing works": the MCP tool surface, the QMP client
(framing, correlation, events, timeouts, disconnects), capture with the PNG/PPM fallback,
coordinate transforms, the keymap and text pre-validation, the bounded single-writer action
transaction with frame fencing and request de-duplication, the input ledger and its cleanup,
overlay creation and reset, path confinement, the audit trail, the lifecycle state machine,
the guest-tool bridge and its policy gates, and the `doctor` report — all covered by tests
that run without a hypervisor, including a real child process for the lifecycle paths.
