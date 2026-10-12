# Palette

**English** | [简体中文](README.zh-CN.md)

Palette gives a visual agent an **observable, operable, resettable virtual desktop** — a
QEMU guest it can see and click on — instead of the host desktop.

Baseline path:

```
Agent → MCP Server → controlled QMP interface → QEMU standard HID / framebuffer
```

Retained enhancement path (same policy, scheduling and audit, off by default):

```
Agent → same MCP Server → Guest Tool Bridge → in-guest tools (native text, launch app)
```

Palette never exposes raw QMP, HMP, host paths, or a general guest shell to the agent. The
agent receives images and sends bounded actions; everything else — authorization, scheduling,
audit — happens behind that boundary and is not addressable from a tool call.

## Status

Implementation of the documented MVP scope. The service, the MCP tool surface, the QMP
client, capture, input, lifecycle, policy and the guest-tool contract are implemented and
covered by tests against a fake QMP server. Physical acceptance (WHPX + a real Debian desktop
image) is **not** claimed: this machine has no QEMU binary, so `palette doctor` reports it as
missing rather than degrading to TCG.

## Quick start

```bash
uv sync
uv run palette doctor                 # probe host, QEMU, acceleration, profile
uv run palette vm start --profile default
uv run palette serve                  # MCP server on stdio
```

See `docs/` for the user guide, operator runbook, guest template build notes and the threat
model.

## Safety defaults

| Control | Default |
| --- | --- |
| Network | offline, no NIC |
| QMP transport | private child-process pipes, never TCP |
| Host paths | generated under `.runtime/`, never agent-supplied |
| Guest enhancement tools | disabled |
| Arbitrary images / QEMU args / ISOs | rejected |
| TCG fallback | disabled; missing acceleration is a diagnosed failure |
| Audit | action type, length, timing, result — never input text or persistent screenshots |
