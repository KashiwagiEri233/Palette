# Palette documentation

**English** | [简体中文](README.zh-CN.md)

These pages are the practical documentation: what the agent may call, how to run the host, how
to build the image, and what is deliberately out of scope. Two rules run through all of them —
the agent gets images and bounded actions and nothing else, and the visual baseline has to keep
working on its own.

| Document | Read it when |
| --- | --- |
| [`../README.md`](../README.md) · [中文](../README.zh-CN.md) | you want the one-paragraph summary and the safety defaults |
| [`user-guide.md`](user-guide.md) · [中文](user-guide.zh-CN.md) | you are the agent or the client author: the tool contracts, the loop, the limits |
| [`operator.md`](operator.md) · [中文](operator.zh-CN.md) | you run the host: install, profiles, lifecycle, emergency stop, recovery |
| [`platforms.md`](platforms.md) · [中文](platforms.zh-CN.md) | you choose Windows/macOS/Linux, a native guest or explicit TCG, firmware and resource limits |
| [`guest-template.md`](guest-template.md) · [中文](guest-template.zh-CN.md) | you are building the desktop image the profile points at |
| [`threat-model.md`](threat-model.md) · [中文](threat-model.zh-CN.md) | you need to know what is trusted, what is enforced, and what is out of scope |
| [`benchmarks.md`](benchmarks.md) · [中文](benchmarks.zh-CN.md) | you are measuring performance, or wondering whether anything has been measured |
| [`known-limitations.md`](known-limitations.md) · [中文](known-limitations.zh-CN.md) | you want the honest list of what does not work yet |
| [`../guest/agent/README.md`](../guest/agent/README.md) · [中文](../guest/agent/README.zh-CN.md) | you are implementing the guest-side enhancement service |

Every page here exists in both languages; the two versions are kept in step by hand, and a
change to one is incomplete until the other is updated too.

## Where things are in the code

| Path | What lives there |
| --- | --- |
| `src/palette/server.py` | the MCP tool surface: schemas, images, structured results |
| `src/palette/service.py` | policy, dispatch, audit and lifecycle in one place |
| `src/palette/policy.py` | path confinement, lifecycle authority, capability gating |
| `src/palette/models.py` | frames, the closed action union, receipts, error payloads |
| `src/palette/qmp/` | the private management channel: transport and protocol |
| `src/palette/vm/` | process, images, overlays, the lifecycle state machine, the process guard |
| `src/palette/capture/` | framebuffer capture and the PNG/PPM fallback |
| `src/palette/input/` | coordinates, keymaps, and the bounded action transaction |
| `src/palette/guest_tools/` | the retained enhancement channel: protocol, disabled backend, bridge |
| `scripts/` | operator tools (`p0_probe.py`, `mcp_stdio_smoke.py`) and `check_docs.py` |
| `guest/` | the offline fixture page and the guest-service contract |
| `profiles/` | trusted configuration templates, with no secrets in them |

## A note on testing

`tests/` is organised by what a failure there would mean:

* `tests/unit` — pure logic: coordinates, keymaps, models, policy, capture decisions, the
  action transaction against in-process doubles;
* `tests/qmp` — the protocol layer against a scripted in-process QMP peer;
* `tests/guest_tools` — the enhancement contract against a mock guest service;
* `tests/integration` — a real child process speaking QMP over real pipes, plus the MCP
  surface driven through the SDK's own tool manager;
* `tests/e2e` — physical acceptance, skipped unless a real QEMU and image are configured.
