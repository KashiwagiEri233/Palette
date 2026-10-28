# Palette Documentation Hub

**English** | [简体中文](README.zh-CN.md)

This directory contains the technical and operational documentation for Palette, covering **Palette Desktop**, the **MCP visual agent tool contract**, **shared daemon and CLI operations**, **cross-platform hardware acceleration**, and the **security threat model**.

Two core invariants run through every page:

1. **Strict Agent Boundary**: Agents never touch raw QMP, HMP, the host desktop or a host shell. Optional Windows enhancement commands execute in the guest under its logged-in user's rights.
2. **Self-Sufficient Visual Baseline**: The visual loop works end-to-end using standard framebuffer captures and standard HID keyboard/pointer events alone, without requiring any in-guest agent.

## Documentation Map

| Document | Audience & Scope |
| --- | --- |
| [Guest Enhancements](guest-enhancements.md) · [简体中文](guest-enhancements.zh-CN.md) | Guest installation, capability detection/degradation, Unicode clipboard, automatic resolution, QGA management, file transfer and private WebDAV exports |
| [Project Overview (../README.md)](../README.md) · [简体中文](../README.zh-CN.md) | Architecture overview, key features, quick start, and safety default switches |
| [Desktop Guide (desktop.md)](desktop.md) · [简体中文](desktop.zh-CN.md) | Running Palette Desktop from source, persistent VM creation, offline hardware settings, read-only RFB gateway, human takeover, and shared daemon usage |
| [Backend Ownership & Concurrency](backend-architecture.md) · [简体中文](backend-architecture.zh-CN.md) | Application boundaries, per-VM operations, disk references and cancellation-safe cleanup |
| [Desktop GUI and Built-in Agent Design (desktop-agent-design.md)](desktop-agent-design.md) · [简体中文](desktop-agent-design.zh-CN.md) | Proposed four-region layouts, component states, takeover/approval sequences, typed IPC draft, theme tokens, and staged acceptance |
| [Agent \& Client User Guide (user-guide.md)](user-guide.md) · [简体中文](user-guide.zh-CN.md) | For agent and MCP client authors: the 10 MCP tools, `vm_observe → vm_act` loop, frame epoch guards, text rules, and error recovery |
| [Operator Runbook (operator.md)](operator.md) · [简体中文](operator.zh-CN.md) | For host administrators: host probing, `daemon` vs `serve`, profiles, lifecycle commands, emergency stop, retention, and fault recovery |
| [Platforms \& Guest Architectures (platforms.md)](platforms.md) · [简体中文](platforms.zh-CN.md) | Native Windows (`WHPX`), macOS (`HVF`), and Linux (`KVM`) profiles, explicit cross-ISA `TCG`, UEFI firmware discovery, and process memory caps |
| [Guest Template Guide (guest-template.md)](guest-template.md) · [简体中文](guest-template.zh-CN.md) | Building, hardening, and SHA-256 pinning the read-only Debian 13 + XFCE base image |
| [Threat Model (threat-model.md)](threat-model.md) · [简体中文](threat-model.zh-CN.md) | Trust boundaries, caller-supplied material switches (`allow_agent_*`), enforced guarantees, and explicit non-goals |
| [End-to-End Acceptance Testing (acceptance-test.md)](acceptance-test.md) · [简体中文](acceptance-test.zh-CN.md) | Full verification workflow from shared `daemon` startup, GUI VM creation, MCP observation/clicks, and human takeover to hardware settings and real test records |
| [Benchmarks (benchmarks.md)](benchmarks.md) · [简体中文](benchmarks.zh-CN.md) | Capture latency, single-click round-trip, cold-boot phase breakdown targets, overhead analysis, and physical correctness criteria |
| [Known Limitations (known-limitations.md)](known-limitations.md) · [简体中文](known-limitations.zh-CN.md) | Honest scope boundaries around IME, clipboard, allowlist networking, in-process deduplication, and multi-monitor |
| [In-Guest Agent Contract (../guest/agent/README.md)](../guest/agent/README.md) · [简体中文](../guest/agent/README.zh-CN.md) | Length-prefixed JSON protocol, capability handshake, and security rules for the optional `virtio-serial` guest service |
| [Guest Images Directory (../images/README.md)](../images/README.md) · [简体中文](../images/README.zh-CN.md) | Base image storage conventions and `palette doctor` verification |

> **Bilingual Pairing Rule**: Every user documentation page ships in both English (`*.md`) and Simplified Chinese (`*.zh-CN.md`). `scripts/check_docs.py` enforces language switchers, resolvable relative links, balanced code fences, and valid UTF-8 in CI.

## Source Code Map

| Path | Responsibility |
| --- | --- |
| `src/palette/cli.py` | Operator CLI entry point: `doctor`, `serve`, `daemon`, `config`, and `vm` lifecycle commands |
| `src/palette/server.py` | MCP tool surface: tool registration, input/output schemas, `ImageContent` encoding, and structured errors |
| `src/palette/service.py` / `contracts.py` / `lifecycle.py` | Typed local/remote business interface, observation/action orchestration and shared lifecycle rules |
| `src/palette/runtime.py` | Shared `RuntimeOwner`: hosts `OperatorServer`, `DesktopManager`, `DisplayGateway`, and MCP remote proxying |
| `src/palette/desktop.py` / `definitions.py` / `persistent_storage.py` | Persistent VM settings, atomic definition storage, disk expansion and recovery |
| `src/palette/vm_settings.py` | Typed VM hardware configuration models (CPU/RAM/disk/boot/display/network/audio) and local QEMU capability probing |
| `src/palette/display.py` | Localhost-only WebSocket RFB read-only display gateway with one-time tickets and client write filtering |
| `src/palette/control.py` | Human-agent exclusive control lease manager: 3-second heartbeat renewal, `controller_epoch` bumps, and input release |
| `src/palette/agent_ipc.py` | Shared daemon discovery and `RemoteService` proxy allowing `palette serve` to attach to an existing owner |
| `src/palette/operator/` | Local operator IPC protocol and transports (Windows current-user Named Pipe / POSIX Unix Socket) |
| `src/palette/policy.py` | `PathGuard` path confinement, lifecycle role authorization, and profile capability switches |
| `src/palette/models.py` | `Frame`, closed `Action` union types, `Receipt`, and structured error payload models |
| `src/palette/qmp/` | Private-pipe QMP protocol client, event dispatch, and command allowlist enforcement |
| `src/palette/vm/` | QEMU command builder, read-only base image & `overlay.qcow2` manager, lifecycle state machine, and process supervision |
| `src/palette/capture/` | Framebuffer capture, display resolution verification, and PNG/PPM fallback |
| `src/palette/input/` | Coordinate space transforms, US hardware keymap table, and bounded action transactions |
| `src/palette/guest_tools/` | Optional guest enhancement channel protocol, handshake validation, disabled backend, and `GuestToolBridge` |
| `desktop/` | Palette Desktop app: `src/main/` (Electron main process), `src/preload/` (typed IPC bridge), `src/renderer/` (React UI & noVNC console) |
| `profiles/` | Trusted hardware and security profile templates (`*.toml`) |
| `scripts/` | Hardware probes (`p0_probe.py`), MCP stdio smoke test (`mcp_stdio_smoke.py`), click test (`mcp_click_test.py`), and doc checker (`check_docs.py`) |

## Test Suite Structure

Backend tests live under `tests/`; desktop tests live under `desktop/tests/` and `desktop/e2e/`:

* **`tests/unit/`**: Pure logic and in-process doubles covering coordinates, keymaps, models, policy, hardware settings validation, display gateway filtering, and action transactions.
* **`tests/qmp/`**: QMP protocol tests against a programmable in-process QMP peer covering framing, timeouts, disconnects, and interleaved events.
* **`tests/guest_tools/`**: Guest enhancement contract tests against a mock transport verifying capability intersection, timeouts, and receipts.
* **`tests/operator_ipc/`**: Local operator IPC framing, payload limits, and Windows Named Pipe adapter tests.
* **`tests/integration/`**: Real supervised child processes speaking QMP over real pipes or AF_UNIX sockets, plus `daemon`, persistent desktop definitions, and the MCP tool surface.
* **`tests/e2e/`**: Physical QEMU hardware acceleration and guest image acceptance tests (skipped unless `PALETTE_E2E=1` is set).
* **`desktop/tests/` & `desktop/e2e/`**: Vitest unit tests for frontend/IPC logic and Playwright Electron end-to-end tests covering VM creation, hardware settings, live console, and shutdown flows.
