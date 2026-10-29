# Latency Benchmarks & Correctness Criteria

**English** | [简体中文](benchmarks.zh-CN.md)

Before reading or reporting performance numbers for Palette, keep two rules in mind:

1. **Targets vs. Measured Results**: The table below defines architectural latency budgets (targets), not universal guarantees across every host. For actual single-host hardware measurements, see the [acceptance test records](acceptance-test.md).
2. **Test Doubles Are Not Benchmarks**: The in-process QMP server and supervised fake QEMU child processes in `tests/` do no virtualization, guest scheduling, or desktop rendering. Timings against them measure only Python framing overhead. **Every benchmark report must label each row with its execution mode (`mock`, `TCG`, `WHPX`, `HVF`, or `KVM`) alongside the host and guest architectures.**

## 1. Running Hardware Measurements

On a host configured with a real QEMU binary and guest base image, run the hardware probe or E2E suite:

```bash
# Run the P0 hardware probe (reports cold boot, observe latency, and click-to-frame timing)
uv run python scripts/p0_probe.py

# Run the real-QEMU E2E test suite against a configured profile
PALETTE_E2E=1 PALETTE_E2E_PROFILE=default uv run pytest tests/e2e -v
```

Standard baseline conditions: one VM, `1280 × 800` resolution, `2` vCPUs, `4 GiB` RAM, and an idle guest desktop. Always record the host CPU model, RAM, and storage type alongside the numbers.

## 2. Latency Targets

| Metric | Definition | Target Budget |
| --- | --- | --- |
| **`vm_observe` Latency** | Request start to MCP `ImageContent` ready across 100 steady-state captures | **P95 ≤ 500 ms** |
| **`vm_act` Single-Click Loop** | Action start to post-action screenshot ready (single click, excluding model inference) | **P95 ≤ 1000 ms** |
| **Cold Boot Breakdown** | Recorded as three separate milestones: `QMP reachable` / `first valid framebuffer (screen_ready)` / `desktop ready` | **Reported separately** (no single combined target across diverse guest OSes) |

> **Note on `type_text`**: Keyboard text entry sends discrete key-down/key-up events paced by `char_interval_ms` (12 ms by default). Typing 256 characters involves hundreds of QMP round-trips under its own budget (`max_total_ms = 15000`) and does not share the single-click latency target.

## 3. Where the Time Goes

When a host misses a latency target, break the elapsed time down across these five stages before adding complexity:

1. **Image Encoding & Resizing (Pillow PNG)**: Compressing raw framebuffer bytes into PNG is typically the single largest CPU cost.
2. **Scratch File I/O**: QEMU `screendump` writes to `.runtime/vms/<vm_id>/<instance_id>/frames/`, which Palette reads back and unlinks immediately.
3. **QMP Transport Round-Trip**: One private transport write (child stdio on POSIX, AF_UNIX on Windows) plus one newline-delimited JSON response from the QEMU main loop.
4. **Post-Input Settle (`settle_after_input_ms`)**: The configured delay between submitting input and capturing the post-action screenshot (`120 ms` by default).
5. **Queue Wait**: Time spent queued behind another in-flight action on the same VM.

## 4. Human Input and Live Display

Human input uses `control.input` → QMP `input-send-event`; it does not incur the Agent's post-input settle or per-action screenshot. Live display uses the separate, read-only VNC/WebSocket stream. Discrete keys/buttons flush in a microtask; motion-only batches use an 8 ms minimum send interval. Waiting for an outstanding ACK or a running transaction is additional latency, not part of that scheduling budget.

Desktop negotiates `operator-input-stream-v1` and opens one lease-bound connection for input. Each batch retains authentication, sequence receipts and epoch checks. Older backends keep one-shot requests. Heartbeats and releases use independent connections; a disconnect never replays uncertain input. At most two input streams occupy the eight local connection slots. Streams close on lease loss, and a started frame must complete within two seconds even though an idle valid lease may remain connected.

For diagnostics, `window.palette.request('control.metrics', VM_ID)` returns rolling P50/P95/P99 milliseconds and sample counts (up to 256 per metric). `input` covers the current instance's queue/QMP/audit/total times; `runtime` reports event-loop lag and `display` reports gateway forwarding/backpressure time across the runtime. These are transport/service metrics, not guest presentation latency. The renderer emits `palette-input-timing` CustomEvents containing only `queue_ms` and `roundtrip_ms`; diagnostic listeners must bound their own history. No key values, text or screenshots are recorded.

Compare the same host/guest before and after at 1280×800 and 1920×1080, both idle and with repeated background `vm_observe`. Record input-to-ACK separately from input-to-visible guest response, plus frame intervals during dragging and large-area scrolling. Use a guest test target that visibly changes on each numbered input; correlate its rendered marker with the input rather than treating the next arbitrary VNC update as a response. Report accelerator, architectures, resolution and sample count. Mock timings verify application overhead only; real motion-to-display latency requires a real guest. Image processing runs in at most two workers and remains serialized with input at the transaction level.

After building Desktop, run `npm exec -- playwright test e2e/human-input.spec.ts` from `desktop/` for the isolated application-level regression. Its attached `human-input-mock.json` contains frontend and backend timing windows explicitly labelled `mock`; it also checks tiny-wheel accumulation and drag release outside the canvas. This report is not a real-QEMU performance result.

## 5. Physical Correctness & Endurance Criteria

These physical acceptance criteria require a real QEMU hypervisor and guest image and have no meaningful mock equivalent:

* **Click Coordinate Accuracy**: Clicking the four corners, center, and at least 200 grid points on `guest/fixtures/offline-workbench.html` lands within **≤ 2 native pixels**.
* **Mid-Session Resolution Change Protection**: Changing guest display resolution mid-session rejects subsequent actions built on pre-resize frames with `STALE_FRAME` rather than clicking the wrong coordinates.
* **20 Start/Stop/Reset Cycles**: Zero orphaned QEMU or supervisor processes, and the read-only base image SHA-256 digest remains unchanged byte-for-byte.
* **30-Minute / 1,000-Action Soak Test**: Mixed moves, clicks, drags, wheel scrolls, key chords, and text entry complete with zero stuck keys, zero descriptor/memory leaks, and bounded `.runtime/` disk usage.
