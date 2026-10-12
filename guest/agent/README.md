# Guest tool service (not implemented)

**English** | [简体中文](README.zh-CN.md)

This directory is the intended home of the in-guest service behind the retained
enhancement channel. **No implementation exists in this build.** The host side is
complete — protocol, framing, version negotiation, capability reconciliation, policy and
a mock — and `VirtioSerialGuestToolTransport` reports "not implemented" rather than
pretending to connect.

The plan's position is deliberate: the enhancement path is defined now, with a disabled
backend and mock contract tests, so that turning it on later is a transport
implementation rather than an architecture change. It is also explicitly *not* reduced to
a clipboard broker, and it is explicitly *not* a general guest shell.

## What a service would have to satisfy

### Transport

* Reachable over a per-instance channel that the host opens privately. The plan's first
  choice is a dedicated virtio-serial port (`org.palette.tools.0`); it must not be a
  listening TCP socket, and it must not share QEMU's QMP stream.
* Length-framed messages: a 4-byte big-endian length followed by UTF-8 JSON, matching
  `palette.guest_tools.protocol`.
* Size and rate limited on both sides.

### Handshake

1. Host sends `hello` with its protocol versions, `vm_id`, `instance_id`, `boot_epoch`,
   a `connect_id`, and the capabilities it intends to use.
2. Service replies `hello_ack` with the version it selected, the capabilities and input
   methods it actually offers, its registered applications, the session it is attached
   to, and whether an interactive desktop is ready.

Both sides must report the *intersection* of what was asked for and what they can do. A
service that advertises a capability it cannot honour is worse than one that offers fewer.

### Operations

| Capability | Request | Notes |
| --- | --- | --- |
| `guest.input_text` | `{text, method, target}` | `method` is `native_text` or `clipboard_paste`; the service must reject a method it does not offer rather than substituting another |
| `guest.launch_app` | `{app_id, options}` | `app_id` resolves to a fixed executable with a bounded set of options. Never a free-form command line, path or argv array, and never a shell |

### Non-negotiable rules

* **Run in the interactive user session.** On Linux, bind to the graphical session's
  environment. Creating a process in the wrong session is not "opening an application".
* **Never escalate.** No automatic `sudo`, no UAC prompt, no inheriting host secrets.
* **Report honestly.** Return `effect_state` (`not_sent`, `submitted`, `partial`,
  `unknown`), the method actually used, and a `launch_id` for a launch. An accepted
  request is not a completed task: a process existing does not mean a window is visible.
* **Treat the host as untrusted in the other direction too.** The service is a small
  attack surface inside the guest; it should validate everything it receives and expose no
  file, shell or process-management operations beyond the two capabilities above.

## Testing a future implementation

Replace `VirtioSerialGuestToolTransport` with the real transport and run:

```bash
uv run pytest tests/guest_tools -v        # contract tests, in-process
uv run pytest tests/integration/test_guest_channel.py -v
```

`MemoryGuestToolTransport` implements the same interface as a real channel and can stall,
disconnect or reorder replies, so the bridge's timeout, correlation and generation logic is
already exercised the way a real service would exercise it.
