# In-Guest Enhancement Service Contract (`guest/agent/`)

**English** | [简体中文](README.zh-CN.md)

This directory defines the protocol specification for the guest-side daemon backing Palette's **Retained Guest Enhancement Channel**.

* **Host-Side Status (Implemented & Tested)**: Length-prefixed framing (`src/palette/guest_tools/protocol.py`), version and capability intersection negotiation, policy enforcement, `GuestToolBridge` (`src/palette/guest_tools/bridge.py`), and an in-process fault-injecting transport (`MemoryGuestToolTransport`) are fully implemented and covered by automated tests.
* **Guest-Side Status (Specification Only; No Binary Bundled)**: Until a guest image runs a daemon implementing this contract over `virtio-serial`, `VirtioSerialGuestToolTransport` reports the channel as offline, `vm_status` reports transport and service readiness as separate facts, and calls to `vm_guest_input_text` or `vm_guest_launch_app` fail cleanly with `GUEST_TOOL_UNAVAILABLE` — **never silently downgrading to simulated keystrokes**.

Keeping the host bridge and contract complete today means that enabling native Unicode/CJK text injection or allowlisted app launching later requires only dropping in a guest daemon and wiring the `virtio-serial` transport, without changing Palette's policy, scheduling, or audit architecture.

## 1. Transport Specification

* **Dedicated Private Channel**: Communicates over a per-instance `virtio-serial` port (`org.palette.tools.0`) opened privately by the host. It must never use a listening TCP socket and must never multiplex onto QEMU's QMP stream.
* **Length-Prefixed Framing**: Each message consists of a **4-byte big-endian unsigned integer (`uint32 BE`)** payload length followed by that many bytes of **UTF-8 JSON** (matching `palette.guest_tools.protocol`).
* **Bounded Bidirectional Limits**: Both host and guest enforce maximum message byte lengths and request rate limits.

## 2. Handshake & Capability Intersection

Upon connection or after a guest `RESET` event, the host and guest service perform an explicit handshake:

1. **Host Sends `hello`**:
   Includes `protocol_version` (`"1.0"`), `vm_id`, `instance_id`, `boot_epoch`, a unique `connect_id`, and the `capabilities` and `input_methods` permitted by the active profile.
2. **Guest Service Replies with `hello_ack`**:
   Reports the selected `protocol_version`, the `capabilities` and `input_methods` the guest service actually implements, its registered `allowed_apps`, the attached session type (`"interactive"`), and whether the interactive desktop is ready (`desktop_ready: true/false`).

> **Intersection Rule**: Both sides enforce the strict **intersection** of what the profile permits and what the guest service actually provides. A service must never advertise a capability or input method it cannot honor.

## 3. Supported Operations

The guest service exposes strictly two bounded operations — **it is neither a general guest shell nor a host-clipboard broker**:

| Capability | MCP Tool | Request Payload | Rules & Constraints |
| --- | --- | --- | --- |
| `guest.input_text` | `vm_guest_input_text` | `{text, method, target}` | `method` is explicitly `"native_text"` (written via the guest OS platform API) or `"clipboard_paste"` (sets the *guest's* clipboard only, never the host's). The service must reject any method it does not implement rather than silently substituting another |
| `guest.launch_app` | `vm_guest_launch_app` | `{app_id, options}` | `app_id` resolves to a fixed executable path and bounded option allowlist registered in both the profile and guest (e.g., `text-editor` → `/usr/bin/mousepad`). **Never accepts free-form command lines, arbitrary paths, or `argv` arrays, and never invokes a shell** |

## 4. Non-Negotiable Security Rules

1. **Run Inside the Interactive User Session**: Bind to the active graphical desktop session (`target_session = "interactive"`). Spawning a process in a detached background session does not count as launching a desktop app.
2. **Never Elevate Privileges**: Run as the standard desktop user — no automatic `sudo`, no UAC elevation prompts, and no inherited host secrets.
3. **Report `effect_state` Honestly**: Return `effect_state` (`not_sent`, `submitted`, `partial`, or `unknown`), the actual `method` used, and a `launch_id` for app launches. A running process does not prove a window appeared on screen — agents still verify visually via post-action screenshots.
4. **Treat the Channel as Untrusted Input**: Validate every incoming JSON payload, enforce `max_text_chars` (4096 by default), and expose zero file-system, shell, or arbitrary process-control endpoints.

## 5. Testing a Guest Service Implementation

After wiring `VirtioSerialGuestToolTransport` to your guest daemon, run the contract and integration suites:

```bash
# In-process protocol, handshake intersection, timeout, and out-of-order reply tests
uv run pytest tests/guest_tools -v

# End-to-end service integration tests for the guest enhancement channel
uv run pytest tests/integration/test_guest_channel.py -v
```
