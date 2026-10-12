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

## Boundaries and what enforces them

| Risk | Control |
| --- | --- |
| An agent using the management interface to reach the host | no `qmp_execute`, no HMP, no generic monitor passthrough. The command allow-list is a reviewed constant, and an unknown command is refused locally without being sent |
| The management channel being reachable from elsewhere | QMP runs over private pipes to a child process the service started. There is no TCP listener, no VNC, no SPICE, no display window |
| Host files being read or written | no argument is ever a host path. Images, frames, logs and the audit trail are composed from trusted configuration and generated identifiers, then confined to `runtime_root` with both a lexical and a link-aware check |
| A guest attacking the host | QEMU runs unelevated with a minimal device set, no shared folders, no passthrough, and a whitelisted environment. Keep it updated; pinning a version is not a reason to skip security patches |
| Data leaking through a shared channel | no 9p, no virtiofs, no SMB, no two-way clipboard, no host device passthrough |
| Reaching the host or the internal network | no NIC at all by default. Restricted user networking is not an egress policy and is not offered as one |
| Resource exhaustion | bounded action queue, bounded text length and typing budget, bounded action duration, bounded image size and pixel count, bounded audit file growth, and a host-side memory ceiling on the QEMU process |
| Two writers fighting over one desktop | one action queue per VM, one controller epoch, no additional writable input path |
| Logs leaking secrets | the audit trail records action type, length, timing and outcome. Never typed text, never launch arguments, never screenshots, never host paths |
| A reset deleting the wrong thing | deletion requires a manifest and a path that resolves inside `runtime_root`, and refuses to follow links |
| A page persuading the agent to escalate | permissions come from the trusted profile. There is no tool that can grant a permission, and no path an agent can supply that becomes a file or a command |

## What Palette does not defend against

* **A malicious process already running as the same host user.** Anything that can read
  this user's files can read the runtime directory and talk to the same QEMU binary.
  Stronger isolation needs a separate low-privilege service account and an OS sandbox —
  a reasonable next step, not something this MVP claims.
* **Multi-tenancy.** One service, one operator, one VM at a time. There is no per-tenant
  authorization, quota or audit separation.
* **Real malware.** A guest is a virtual machine, not a proof. Treat it as a containment
  measure, not a sandbox for hostile code.
* **Screenshots staying private.** A screenshot is sent to whichever model the client
  configured. Not writing it to disk does not mean nobody saw it. Use test accounts and a
  template with nothing sensitive in it, and assume anything on screen reached the model.
* **The remote transport.** This build serves stdio only. Streamable HTTP would need TLS,
  authentication, per-VM authorization, strict `Origin`/`Host` validation, message limits,
  rate limits, session expiry and audit — and an MCP session id is not an identity.
* **Guaranteed exactly-once.** De-duplication is in-process and bounded. A crash between
  sending input and recording the result leaves an action whose outcome nobody knows; the
  service reports `unknown` rather than replaying it.
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
* `system_reset` is never treated as a reset: a clean reset replaces the writable layer and
  leaves the base image byte-for-byte identical.
