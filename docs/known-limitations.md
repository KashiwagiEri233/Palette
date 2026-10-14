# Known Limitations & Scope Boundaries

**English** | [简体中文](known-limitations.zh-CN.md)

Every item on this page is either a **deliberate architectural boundary** or a **real technical constraint** in the current release. Palette reports these boundaries honestly at runtime — unsupported operations fail with explicit error codes rather than silently degrading or faking success.

## 1. Host Environment & Platform Constraints

* **No Bundled Guest OS Images**: The repository ships trusted profile templates (`profiles/*.toml`), while read-only base images and installer ISOs are built or supplied by the operator and pinned by SHA-256 digest (see the [guest template guide](guest-template.md)).
* **No Silent TCG Fallback**: Native profiles support Windows (`WHPX`), macOS Intel / Apple Silicon (`HVF`), and Linux x86_64 / aarch64 (`KVM`); cross-ISA software emulation requires explicitly selecting `tcg-x86_64` or `tcg-aarch64`. Missing hardware acceleration fails loudly with `ACCELERATION_UNAVAILABLE`.
* **Sampled Memory Limit on POSIX**: Windows enforces `process_memory_limit_mib` as a hard kernel ceiling via a Job Object. On macOS and Linux, the POSIX supervisor samples total process-tree RSS every **250 ms** and kills the process group when exceeded, permitting brief transient overshoot. Process-group cleanup also cannot contain a host program that deliberately calls `setsid` to escape its session (which QEMU itself does not do when `allow_agent_qemu_args = false`).
* **Physical Guest Acceptance Is Host-Specific**: Default CI suites run against supervised fake QEMU child processes and local pipes/RFB sockets to verify framing, policy, and UI state machines; physical guest compatibility must be verified on each target host and image (see the [acceptance test guide](acceptance-test.md)).

## 2. Desktop Interaction & Device Limitations

* **No Direct Unicode / CJK Text in Visual Baseline Mode**: Baseline `vm_act` `type_text` (`mode: "keyboard"`) emits **US-layout printable ASCII** key scancodes only. Control characters (`
`, `	`) and non-ASCII characters fail pre-validation with `UNSUPPORTED_TEXT` before any key is pressed. Similarly, live human typing in the Palette Desktop console sends physical key scancodes rather than host IME composition strings.
* **In-Guest Enhancement Daemon (`guest/agent/`) Is a Contract Only**: The host-side protocol framing, version negotiation, policy switches, and `GuestToolBridge` are fully implemented and covered by mock contract tests, but no in-guest `virtio-serial` binary is bundled yet. Calling `vm_guest_input_text` or `vm_guest_launch_app` against a guest without a running service fails cleanly as `GUEST_TOOL_UNAVAILABLE`.
* **`type_text` Is Paced Per Keystroke**: Typing sends discrete key-down/key-up events paced by `char_interval_ms` (12 ms by default), so 256 characters require hundreds of round-trips and use a dedicated timeout budget (`max_total_ms = 15000`).
* **Vertical Scroll Only (`up` / `down`)**: Horizontal scroll is intentionally omitted from `vm_act` because it is not part of the verified guest device baseline.
* **No Touch, Gestures, or Relative Pointer Lock**: Pointer positioning uses absolute coordinates via `usb-tablet`; multi-touch gestures, stylus pressure, and relative pointer lock are not supported.
* **No Host Clipboard Sharing**: Neither Palette Desktop nor MCP bridges the host clipboard into the guest.
* **Output-Only Audio; No Microphone, Passthrough, or Shared Folders**: Palette Desktop supports **Intel HDA audio output** to the host's default audio device (when supported by the local QEMU build and guest drivers), but **never captures the host microphone** and never exposes webcam passthrough, USB/PCI passthrough, multi-monitor setups, or `9p`/`virtiofs`/SMB shared directories.
* **`vm_wait` Change & Stability Modes Are Pixel Heuristics**: `changed` and `stable` compare 64×64 thumbnails; blinking cursors, clocks, or spinners can trigger or defeat them, and neither proves an application is logically idle.

## 3. State, Protocol & Correctness Boundaries

* **`allow_agent_qemu_args = true` Removes the Host Boundary**: Allowing callers to append raw QEMU flags permits host file read/write and secondary control channels (see the [threat model](threat-model.md)). Set it to `false` (as in `profiles/strict-example.toml`) whenever the caller is untrusted.
* **Pixel Checks Do Not Detect Semantic UI Shifts**: Frame validation guarantees that a click lands on the exact pixel coordinate of the image you observed, but a web page can move a button between two identical-looking captures. Gate payments, deletions, and submissions behind your own confirmation policy.
* **In-Process Deduplication Only**: `request_id` deduplication lives in bounded memory for the lifetime of the service process. Across a service crash or pipe disconnect, the honest outcome is `unknown` (`ACTION_OUTCOME_UNKNOWN`), and Palette reports that rather than replaying input.
* **Private Anonymous QMP Pipes Cannot Be Reattached Across Service Restarts**: When `daemon` or `serve` exits, the Windows Job Object or POSIX supervisor reaps its QEMU child processes. Restarting the service requires starting a new VM instance (persistent Desktop VMs keep their writable disk and UEFI variables across restarts).
* **Hardware Cursor Position Is Not Observed**: Unless Palette moved the pointer during the current instance, `cursor_position_source` is reported as `not_observed`.
* **`screendump` Is Not an Atomic Framebuffer Transaction**: Rapid guest screen updates during a dump can occasionally produce a torn frame.

## 4. Networking & Operational Boundaries

* **Local-Only Transports; No Multi-Tenant Remote API**: MCP runs over `stdio`, operator/desktop IPC binds to a local Unix Socket or current-user Named Pipe, and the display gateway binds strictly to `127.0.0.1`. There is no multi-tenant isolation or remote HTTP transport.
* **No Cross-VM Job Scheduler**: A single runtime owner can manage multiple VMs concurrently (each with its own serialized queue and control lease), but cross-VM task scheduling stays with the caller.
* **No Enforced Egress Allowlist Under User-Mode NAT**: `offline`, `restricted-lab` (`restrict=on`), and `nat` (with localhost-only port forwards) are supported. `internet-allowlist` is refused at start rather than approximated with slirp user-mode NAT.
