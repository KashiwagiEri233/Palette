# In-Guest Enhancement Service Contract (`guest/agent/`)

**English** | [简体中文](README.zh-CN.md)

This directory defines the protocol specification for the guest-side daemon backing Palette's **Retained Guest Enhancement Channel**.

* **Host-Side Status (Implemented & Tested)**: Length-prefixed framing (`src/palette/guest_tools/protocol.py`), version and capability intersection negotiation, policy enforcement, `GuestToolBridge` (`src/palette/guest_tools/bridge.py`), and an in-process fault-injecting transport (`MemoryGuestToolTransport`) are fully implemented and covered by automated tests.
* **Windows guest implementation**: `guest/windows/Palette.*.cs` implements a SYSTEM transport broker and an ordinary-user worker. The tools disc compiles it offline with the inbox .NET Framework compiler and installs the broker service. The real `VirtioSerialGuestToolTransport` uses Palette's per-instance private socket/named pipe. Missing services remain unavailable and never silently downgrade Unicode to keystrokes.

See the [enhancement guide](../../docs/guest-enhancements.md) for installation, tool arguments, permissions, limits and the remaining real-VM acceptance requirements.

## 1. Transport Specification

* **Dedicated Private Channel**: Communicates over a per-instance `virtio-serial` port (`org.palette.tools.0`) opened privately by the host. It must never use a listening TCP socket and must never multiplex onto QEMU's QMP stream.
* **Length-Prefixed Framing**: Each message consists of a **4-byte big-endian unsigned integer (`uint32 BE`)** payload length followed by that many bytes of **UTF-8 JSON** (matching `palette.guest_tools.protocol`).
* **Bounded Bidirectional Limits**: Both host and guest enforce maximum message byte lengths and request rate limits.

## 2. Handshake & Capability Intersection

Upon connection or after a guest `RESET` event, the host and guest service perform an explicit handshake:

1. **Host Sends `hello`**:
   Bootstrap envelope `v="1.0"`, `kind="hello"`, `id=connect_id`; payload contains `versions=["1.1","1.0"]`, `vm_id`, `instance_id`, `boot_epoch`, unique `connect_id` and `desired_capabilities`. Later requests use the negotiated version.
2. **Guest Service Replies with `hello_ack`**:
   Reports selected `version`, implemented `capabilities`/`input_methods`, registered `apps`, `session="interactive"` and `interactive_desktop`. The Windows worker requires 1.1; the host retains 1.0 support for older implementations and restricts those connections to legacy operations.

> **Intersection Rule**: Both sides enforce the strict **intersection** of what the profile permits and what the guest service actually provides. A service must never advertise a capability or input method it cannot honor.

## 3. Supported Operations

Operations have explicit registered capability categories. The Windows worker provides native text, filesystem, PowerShell, process, registry, App, UIA snapshot, label resolution and guest-only clipboard. Requests contain `capability`, typed `params`, `session`, `boot_epoch`, connection `generation`, `connect_id` and `timeout_ms`; responses contain `ok/effect_state/method/detail/data`. New categories require both a host registry entry and a Profile grant. The following two interfaces retain their existing contracts:

| Capability | MCP Tool | Request Payload | Rules & Constraints |
| --- | --- | --- | --- |
| `guest.input_text` | `vm_guest_input_text` | `{text, method, target}` | `method` is explicitly `"native_text"` (written via the guest OS platform API) or `"clipboard_paste"` (sets the *guest's* clipboard only, never the host's). The service must reject any method it does not implement rather than silently substituting another |
| `guest.launch_app` | `vm_guest_launch_app` | `{app_id, options}` | `app_id` resolves to a fixed executable path and bounded option allowlist registered in both the profile and guest (e.g., `text-editor` → `/usr/bin/mousepad`). **Never accepts free-form command lines, arbitrary paths, or `argv` arrays, and never invokes a shell** |

## 4. Non-Negotiable Security Rules

1. **Run Inside the Interactive User Session**: Bind to the active graphical desktop session (`target_session = "interactive"`). Spawning a process in a detached background session does not count as launching a desktop app.
2. **Never Elevate Privileges**: Run as the standard desktop user — no automatic `sudo`, no UAC elevation prompts, and no inherited host secrets.
3. **Report `effect_state` Honestly**: Return `effect_state` (`not_sent`, `submitted`, `partial`, or `unknown`), the actual `method` used, and a `launch_id` for app launches. A running process does not prove a window appeared on screen — agents still verify visually via post-action screenshots.
4. **Treat the Channel as Untrusted Input**: Validate every JSON payload and limit output; native text obeys `max_text_chars` (4096 by default). File/system/App endpoints execute only in the guest and remain individually capability-gated. The SYSTEM broker forwards messages and never executes them.

The host sends `kind="cancel"` with the original request ID and connection identity on timeout or cancellation. The broker stops the worker and any command job; it never replays an operation. A fresh worker re-handshakes before the next request. Host scheduling and deduplication preserve partial/unknown outcomes, while batch mouse/keyboard events stay in the host QMP input ledger. Label resolution itself sends no input.

## 5. Testing a Guest Service Implementation

Run the contract, compiled Windows worker and integration suites:

```bash
# In-process protocol, handshake intersection, timeout, and out-of-order reply tests
uv run pytest tests/guest_tools -v

# End-to-end service integration tests for the guest enhancement channel
uv run pytest tests/integration/test_guest_channel.py -v
```
