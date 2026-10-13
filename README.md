# Palette

The Electron desktop application supports VM creation, persistent disks and live human
control. See the [desktop guide](docs/desktop.md) for source setup and MCP sharing.

**English** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/KashiwagiEri233/Palette/actions/workflows/ci.yml/badge.svg)](https://github.com/KashiwagiEri233/Palette/actions/workflows/ci.yml)

Palette gives a visual agent an **observable, operable, resettable virtual desktop** — a
QEMU guest it can see and click on — instead of the host desktop.

Baseline path:

```
Agent → MCP Server → controlled QMP interface → QEMU standard HID / framebuffer
```

Retained enhancement path (same policy, scheduling and audit; requires a guest-side service):

```
Agent → same MCP Server → Guest Tool Bridge → in-guest tools (native text, launch app)
```

Palette never exposes raw QMP, HMP, or a general guest shell to the agent. The agent receives
images and sends bounded actions; everything else — authorization, scheduling, audit — happens
behind that boundary and is not addressable from a tool call. Host paths and QEMU arguments are
reachable only through the explicit switches below, and never as a side effect of another call.

## Status

Implementation of the documented MVP scope. The service, the MCP tool surface, the QMP
client, capture, input, lifecycle, policy and the guest-tool contract are implemented and
covered by tests against a fake QMP server and real supervised child processes. Profiles support
native Windows/WHPX, macOS/HVF and Linux/KVM, plus explicitly selected x86_64 or aarch64 TCG.
This change has **not** undergone physical guest acceptance on that platform matrix. Tests
against the fake emulator do not establish desktop compatibility or latency.

## Quick start

```bash
uv sync
uv run palette doctor                 # probe host, QEMU, acceleration, profile
uv run palette serve                  # leave running, or let the MCP client launch it
```

In another terminal, using the same configuration root:

```bash
uv run palette vm start               # uses the host's native profile
uv run palette vm status
uv run palette vm stop
```

`serve` owns the VMs. Lifecycle CLI commands contact that running process; they do not start
a temporary service. Configure a matching guest image first. The default profile selector is
`auto`; see the [platform guide](docs/platforms.md) for prerequisites and explicit profile IDs.
On Apple Silicon, `--profile tcg-x86_64` emulates an x86_64 guest without requiring Rosetta.

See `docs/` for the user guide, operator runbook, guest template build notes and the threat
model. Read `docs/threat-model.md` before operating this: three of the defaults below hand the
caller a boundary that the service cannot enforce afterwards.

## Safety defaults

Every row is a switch in a profile, not a hard rule. The shipped `profiles/default.toml` takes
the permissive side of each one; `profiles/strict-example.toml` closes all of them, and is the
same machine otherwise.

| Control | Default | What it means |
| --- | --- | --- |
| Network | virtual NIC, `restricted-lab` | A NIC is attached through user-mode networking with `restrict=on`, so the guest cannot route past the sandbox. `nat` gives it the host's outbound reach and no egress policy; `offline` attaches nothing. An enforced allowlist is not implemented and is refused |
| Host material: image | allowed | A caller may name the base image, which bypasses the profile's pinned path and digest |
| Host material: ISO / extra disk | allowed | A caller may attach a CD-ROM or disk from any host path |
| Host material: QEMU arguments | allowed | A caller may append raw arguments, which **removes the host boundary entirely**: `-chardev`, `-device` and `-blockdev` can read and write host files, and `-mon`/`-vnc` add a second way to drive the guest |
| QMP transport | private child-process pipes, never TCP | Unchanged by the switches above |
| Palette's own paths | generated under `.runtime/`, never caller-supplied | Unchanged: what the switches change is what QEMU is pointed at, not where Palette cleans up |
| Guest enhancement tools | enabled | Costs nothing until a guest-side service answers the handshake; a call against a guest with no service fails as unavailable rather than becoming keystrokes |
| Agent lifecycle | start only | Start is open; stop, force-stop and reset stay with the operator |
| TCG fallback | disabled | Missing acceleration is a diagnosed failure. A caller passing `-accel tcg` explicitly would still get it — argument-level guards stop being guarantees once arguments are open |
| Process lifetime and memory | host-specific | Windows uses a Job Object; macOS/Linux use a supervisor and 250 ms RSS sampling. The POSIX memory budget permits transient overshoot |
| Audit | action type, length, timing, result | Never input text or persistent screenshots; caller-supplied paths go into the instance manifest, not the log |
