# Threat model

**English** | [简体中文](threat-model.zh-CN.md)

What Palette defends against, what it assumes, and what it deliberately does not do.

## Trust

**Untrusted, always:**

* anything the agent sends — arguments, coordinates, text, `request_id`s, and any claim it
  makes about permission;
* the guest operating system and every application inside it, including a guest that
  attempts to attack the host;
* web pages, documents and any text visible on screen;
* a future remote client.

**Trusted:**

* the profile the operator wrote, the permission policy, this service;
* the QEMU build the operator installed, and the host OS.

Screen content is **data**. An instruction that appears on screen is not an instruction
from the operator, and nothing in the service treats it as one.

## Boundaries a caller can open

Three profile switches hand a caller material that the profile would otherwise have pinned.
They are off in `profiles/strict-example.toml` and on in `profiles/default.toml`, and the
consequences are not equivalent — the third one is a different category of thing.

| Switch | What it grants | What the service can still do about it |
| --- | --- | --- |
| `allow_agent_images` | A caller names the base image, so the disk QEMU boots is not the one the profile pinned | Nothing about the file's contents. The writable layer is still per-instance, still ours, and still discarded by reset — but the guest may be anything |
| `allow_agent_iso` | A caller attaches a CD-ROM or extra disk from any host path | Nothing. The file is opened read-only and its path is recorded in the instance manifest |
| `allow_agent_qemu_args` | A caller appends raw QEMU arguments | **Nothing.** `-chardev`, `-device` and `-blockdev` can read and write host files; `-mon`, `-vnc` and `-chardev socket` add a second way to drive the guest, and the single-writer guarantee silently stops holding |

With `allow_agent_qemu_args` on, **every argument-level control in this document becomes
advisory**: the accelerator is whatever the caller appends last, `-display none` can be
joined by a VNC server, and the whitelisted environment protects a process that now runs
whatever it was told to. A service that cannot read the caller's intent cannot filter for it,
and pretending otherwise — a blocklist of "dangerous" flags — would be worse than saying so,
because it would be bypassed by the first flag nobody thought to list.

The service still adds one thing the caller cannot take away: a monitor or display channel
introduced through extra arguments is detected and recorded in the plan notes and the log, so
a broken single-writer guarantee is visible rather than silent.

## Boundaries and what enforces them

| Risk | Control |
| --- | --- |
| An agent using the management interface to reach the host | no `qmp_execute`, no HMP, no generic monitor passthrough. The command allow-list is a reviewed constant, and an unknown command is refused locally without being sent. A caller can still reach QEMU's own argument surface if `allow_agent_qemu_args` is on — see above |
| The management channel being reachable from elsewhere | QMP runs over private pipes to a child process the service started. There is no TCP listener, no VNC, no SPICE, no display window — unless a caller appends one |
| Host files being read or written | No *managed* path is ever caller-supplied: images, frames, logs and the audit trail are composed from trusted configuration and generated identifiers, then confined to `runtime_root` with both a lexical and a link-aware check. Caller-supplied images, CD-ROMs and QEMU arguments are a deliberate exception and are outside that confinement by construction |
| A guest attacking the host | QEMU runs unelevated with a minimal device set, no shared folders, no passthrough, and a whitelisted environment. Keep it updated; pinning a version is not a reason to skip security patches. A caller-supplied device or chardev is outside this |
| Data leaking through a shared channel | none of 9p, virtiofs, SMB, two-way clipboard or host device passthrough is configured by Palette. A caller passing the matching arguments is their decision |
| Reaching the host or the internal network | `restricted-lab` keeps the guest inside the slirp sandbox and `offline` attaches no NIC at all. `nat` gives the guest the host's outbound reach with no egress policy, and a configured `hostfwd` punches through the sandbox in the other direction. An enforced allowlist is refused rather than approximated |
| Resource exhaustion | bounded action/text/capture/audit budgets and argument sizes; Windows Job Object memory limits or POSIX aggregate RSS sampling every 250 ms. POSIX permits temporary overshoot; see [platforms](platforms.md) |
| QEMU surviving its service | Windows kill-on-close Job Object or a POSIX supervisor watching a private liveness pipe, with process-group cleanup |
| Two writers fighting over one desktop | one action queue per VM, one controller epoch, no additional writable input path. Extra arguments that add one are logged, not prevented |
| Logs leaking secrets | the audit trail records action type, length, timing and outcome. Never typed text, never launch arguments, never screenshots, never host paths — caller-supplied paths go into the instance manifest, which lives and dies with the instance |
| A reset deleting the wrong thing | deletion requires a matching manifest and a confined, link-checked path; process exit must be confirmed. External caller files are never deleted, and managed layers still referenced by retained/running disks are protected |
| A page persuading the agent to escalate | permissions come from the trusted profile. There is no tool that can grant a permission. Caller-supplied *arguments* are the one place a request can widen its own reach — which is why that switch exists separately and defaults where it does |

## What Palette does not defend against

The CLI's operator channel is local: a Unix socket on POSIX or a current-user protected
named pipe on Windows, with a runtime-scoped token and service generation. It shares the
existing service and does not expose QMP or accept an agent-supplied operator role. A
process already running as that same OS user is outside this boundary.

* **A malicious process already running as the same host user.** Anything that can read
  this user's files can read the runtime directory and talk to the same QEMU binary.
  Stronger isolation needs a separate low-privilege service account and an OS sandbox —
  a reasonable next step, not something this MVP claims.
* **Multi-tenancy.** One service runs under one host user and operator authority, including
  when it owns several VMs. There is no per-tenant authorization, quota or audit separation.
* **Real malware.** A guest is a virtual machine, not a proof. Treat it as a containment
  measure, not a sandbox for hostile code.
* **Screenshots staying private.** A screenshot is sent to whichever model the client
  configured. Not writing it to disk does not mean nobody saw it. Use test accounts and a
  template with nothing sensitive in it, and assume anything on screen reached the model.
* **The remote transport.** MCP uses stdio; the operator channel is local only. Streamable HTTP would need TLS,
  authentication, per-VM authorization, strict `Origin`/`Host` validation, message limits,
  rate limits, session expiry and audit — and an MCP session id is not an identity.
* **Guaranteed exactly-once.** De-duplication is in-process and bounded. A crash between
  sending input and recording the result leaves an action whose outcome nobody knows; the
  service reports `unknown` rather than replaying it.
* **A caller who may supply QEMU arguments.** Once `allow_agent_qemu_args` is on, the host
  boundary is the caller's to keep, and every argument-level control in this document is
  advisory. This is the one item on this list that is a configuration choice rather than a
  limit of the design: turn the switch off and the boundary is back.
* **A caller who may supply a base image.** The guest is then whatever that file contains.
  Treat an image from an untrusted source exactly as you would treat running it directly.
* **Tool annotations as authorization.** `readOnlyHint` and `destructiveHint` help a client
  present a decision. They replace nothing: every call is checked server-side.

## Guarantees the service does make

* Input is never sent to an instance other than the one the request names, or from a frame
  that another action has already invalidated.
* A timeout, a lost response or a failed follow-up screenshot **never** causes an automatic
  replay. The receipt says `unknown`, and the decision to try again is the caller's.
* A release is only reported as confirmed when the service actually confirmed it. A dead
  channel produces "cannot confirm", never a claim of success.
* Cleanup only touches objects registered in a manifest and confined to the runtime root.
  External caller-supplied files are opened read-only and are never deleted. A managed
  layer used as another image's backing store is protected while that dependency is retained.
* Host-material switches are authorised one at a time. Supplying an image does not imply the
  right to supply arguments, and each refusal names the switch that would have allowed it.
* `system_reset` is never treated as a reset: a clean reset replaces the writable layer and
  leaves the base image byte-for-byte identical. With a caller-supplied base the same holds
  for that file. A guest `RESET` event invalidates frames and guest-tool sessions immediately;
  reconnecting the enhancement channel does not block the visual baseline.
