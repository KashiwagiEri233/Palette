# Security Threat Model

**English** | [简体中文](threat-model.zh-CN.md)

This document defines what Palette trusts, which boundaries are controlled by explicit profile switches, what invariants the service enforces, and what lies outside its scope.

## 1. Trust Boundaries

### Always Untrusted

* **Everything sent by an Agent or MCP Client**: tool arguments, coordinates, text strings, `request_id`, `frame_id`, and any claim about permissions or identity.
* **The Guest OS and Every Application Inside It**: including a compromised guest attempting to attack the host.
* **Everything Visible on the Guest Screen**: web pages, documents, pop-ups, terminal output, or images. **Screen content is strictly data, never instructions from the operator.**
* **Future remote clients**.

### Trusted

* **Operator-Authored Configuration**: `palette.toml`, `profiles/*.toml`, policy rules, and the Palette service code.
* **The Host Environment**: the operator-installed QEMU build, firmware files, and the host OS kernel.

## 2. Boundaries Controlled by Profile Switches

Three switches under `[lifecycle]` hand host material to the caller. All three are **closed** in `profiles/strict-example.toml` and **open** in `profiles/default.toml` for local experimentation. Their security consequences differ sharply:

| Profile Switch | What It Grants the Caller | What the Service Can Still Enforce |
| --- | --- | --- |
| `allow_agent_images` | Caller may pass a custom base image path to `vm_start`, bypassing the profile's pinned path and SHA-256 digest | Writable overlays (`overlay.qcow2`) remain per-instance inside `.runtime/` and are discarded on reset, but the guest OS contents are whatever resides in that file |
| `allow_agent_iso` | Caller may attach read-only CD-ROMs (`iso`) or `extra_disks` from arbitrary host paths | Files are opened read-only and recorded in the instance `manifest.json` (never deleted on cleanup), but the guest can read whatever host files were attached |
| `allow_agent_qemu_args` | Caller may append raw QEMU command-line flags (`qemu_args`) | **None.** `-chardev`, `-device`, and `-blockdev` can read and write arbitrary host files; `-mon`, `-vnc`, and `-chardev socket` open a second control channel that silently bypasses the single-writer guarantee |

> **Why Not a Blacklist for `qemu_args`?**
> Once a caller can pass raw flags to QEMU, every argument-level guard in this document becomes advisory. Maintaining a blocklist of "dangerous flags" is worse than stating the boundary plainly, because the first unblocked flag alias bypasses it. Instead, Palette makes `allow_agent_qemu_args` an explicit switch: turn it `false` to restore the host boundary. Even when open, Palette inspects appended flags and logs any extra monitor or display channels in the startup plan and audit trail so boundary changes are never invisible.

## 3. Enforced Controls & Mitigations

When `allow_agent_qemu_args = false`, Palette enforces the following controls:

| Risk | Enforced Mitigation |
| --- | --- |
| **Agent reaching the host via management APIs** | No `qmp_execute`, no HMP, and no generic monitor passthrough. The QMP client enforces a static allowlist of reviewed commands; unknown commands are rejected locally before touching the pipe |
| **Management channel exposed on the network** | QMP runs strictly over private anonymous pipes to the child process spawned by Palette — never over TCP, VNC, or SPICE sockets |
| **Live display stream abused to inject input** | Palette Desktop's `DisplayGateway` binds strictly to localhost, requires a one-time display ticket, and drops all client write messages at the RFB protocol layer |
| **Path traversal or accidental host file deletion** | Managed paths (`overlay.qcow2`, transient frames, logs, audit trail, persistent definitions) are constructed from trusted config and generated IDs, then verified by `PathGuard` (both lexical confinement and symlink/junction/reparse-point checks) to stay inside `runtime_root` |
| **Data exfiltration via shared host channels** | Palette never configures `9p`, `virtiofs`, SMB shares, bidirectional host clipboards, host USB/PCI passthrough, or microphone capture, and scrubs the QEMU child process environment via an allowlist |
| **Guest probing the host or local network** | `offline` attaches no NIC; `restricted-lab` uses QEMU user-mode networking with `restrict=on` so the guest cannot route out of the sandbox; `internet-allowlist` is refused at start rather than approximated |
| **Resource exhaustion or orphaned QEMU processes** | Bounded action durations, text lengths, capture sizes, queue depths, and IPC payloads; Windows uses a Kill-on-Close Job Object with kernel memory limits, and POSIX uses a dedicated process-group supervisor with 250 ms RSS sampling |
| **Competing writers on the same desktop** | Single serialized action queue per VM and a single-writer input controller; human takeover in Palette Desktop or `palette vm take-control` increments `controller_epoch` and retires all prior agent frames |
| **Secrets leaking into logs** | `audit/palette.jsonl` records only action type, length, timing, outcome, and error code — **never** input text, launch arguments, persistent screenshots, or host paths |
| **Prompt injection on screen escalating privileges** | Permissions come strictly from the static profile on disk; no MCP tool can grant permissions or assume the `operator` role |

## 4. Explicit Non-Goals

* **Malicious Processes Running as the Same Host OS User**: Local operator IPC uses current-user-restricted Unix Sockets (POSIX) or Named Pipes (Windows) with per-runtime tokens. Any process already running as the same OS user can read `.runtime/` or invoke QEMU directly. Defending against same-user processes requires a separate low-privilege service account and OS sandbox.
* **Multi-Tenancy**: A single Palette service instance runs under one host user and operator authority without per-tenant quotas or isolation.
* **Hostile Malware Sandboxing**: Treat the guest VM as an operational containment boundary for agents, not a formally verified zero-day malware sandbox (especially under TCG software emulation, for which QEMU provides no security isolation guarantee).
* **Hiding Screen Content from the Vision Model**: Screenshots are captured specifically to be sent to the configured model. Not persisting screenshots to disk does not mean no one saw them — always use test accounts and non-sensitive templates inside the guest.
* **Cross-Crash Exactly-Once Execution**: `request_id` deduplication is bounded and in-memory for the life of the service process. If a crash or disconnect occurs between sending input and recording the result, Palette reports `unknown` (`ACTION_OUTCOME_UNKNOWN`) rather than replaying the action.
* **Tool Annotations as Authorization**: MCP `readOnlyHint` and `destructiveHint` help clients render UI prompts; all real authorization is enforced server-side on every call.

## 5. Summary of Enforced Guarantees

1. **No Cross-Instance or Stale-Frame Input**: Input is never sent to the wrong `instance_id` or against a `frame_id` invalidated by another action, resize, boot, or control takeover.
2. **Never Replay Unknown Outcomes**: Timeouts, broken pipes, or failed post-action captures never trigger automatic input replay.
3. **Honest Input Release Reporting**: `release_confirmed` is reported as `true` only after the underlying channel confirms release.
4. **Byte-for-Byte Base Image Immutability**: `vm_reset` and Desktop **Restore Initial State** replace the writable `overlay.qcow2` and per-VM UEFI variable copy inside `.runtime/` without ever modifying or deleting the read-only base image.
