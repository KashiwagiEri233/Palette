# Known limitations

**English** | [简体中文](known-limitations.zh-CN.md)

An honest list. Everything here is a deliberate scope boundary or a real constraint, not an
oversight, and none of it is hidden behind a friendlier message at runtime.

## Environment

* **No physical acceptance has been run.** This repository was developed on a host with no
  QEMU installation. `palette doctor` reports that plainly, and `tests/e2e` skips. Every
  claim about WHPX, real capture latency and guest behaviour is unverified.
* **Windows and WHPX only.** The KVM path is designed for — the transport and guard
  interfaces are in place — but not implemented. `NullProcessGuard` reports that orphan
  protection is unavailable rather than pretending otherwise.
* **No guest image ships.** The base image is an operator artefact with a recorded origin
  and digest.

## Capability

* **Unicode text is not supported in visual mode.** Strict keyboard mode covers US-layout
  printable ASCII. Chinese, emoji and multi-line text need the guest enhancement channel,
  which this build defines but does not implement.
* **`type_text` is slow by design.** One input message per key phase, rate limited, so a
  256-character string is hundreds of round trips. It does not share the click latency
  budget.
* **One VM per service process.** The manager is written for several, but nothing owns
  scheduling between them.
* **Vertical scrolling only.** Horizontal scrolling is expressible in QAPI and deliberately
  not exposed: it is untested, and offering it "because the enum exists" would be a claim
  without evidence.
* **No touch, gesture or pressure input.** No multi-touch, no relative pointer lock.
* **`vm_wait` change and stability are pixel heuristics.** A blinking cursor, a clock or an
  animation defeats both. Neither means "the application is idle".
* **No clipboard.** Not the host's, and not by default the guest's either.
* **No audio, no video, no USB passthrough, no shared folders.**

## Correctness boundaries

* **Pixel comparison cannot detect semantic change.** Two identical-looking screenshots can
  hide a moved button. Payment, deletion and submission still need the caller's own
  confirmation policy.
* **Exactly-once is in-process only.** De-duplication is bounded memory inside one service
  lifetime. Across a crash, the honest answer is `unknown`, and the service says so instead
  of replaying.
* **A private anonymous pipe cannot be re-adopted.** If the service process dies, that QEMU
  is not recoverable; the plan accepts this for the MVP.
* **Frames are not archived.** Only a 64×64 thumbnail per frame is retained, and only to
  answer "has the screen changed?".
* **The pointer's real position is unknown.** `cursor_position_source` is `not_observed`
  unless the service moved it, and it never claims to have observed the cursor.
* **`screendump` is not an atomic desktop snapshot.** Updates during the write can produce a
  torn image. It is not presented as a transactional view.

## Operations

* **No remote transport.** stdio only. Streamable HTTP would need TLS, authentication,
  per-VM authorization, strict `Origin`/`Host` validation, limits and audit.
* **No multi-tenant isolation.** One operator, one host user. See the threat model for what
  that does and does not buy.
* **No scheduling platform.** One client controls one VM; `vm_id` exists so that several can
  be addressed, not so that they can be orchestrated.
* **Guest enhancement tools are disabled by default** and unimplemented. The contract, the
  policy gates and the mock exist; the transport does not.

## What is implemented and tested

So the list above is not read as "nothing works": the MCP tool surface, the QMP client
(framing, correlation, events, timeouts, disconnects), capture with the PNG/PPM fallback,
coordinate transforms, the keymap and text pre-validation, the bounded single-writer action
transaction with frame fencing and request de-duplication, the input ledger and its cleanup,
overlay creation and reset, path confinement, the audit trail, the lifecycle state machine,
the guest-tool bridge and its policy gates, and the `doctor` report — all covered by tests
that run without a hypervisor, including a real child process for the lifecycle paths.
