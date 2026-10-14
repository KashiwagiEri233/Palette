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
3. **QMP Pipe Round-Trip**: One pipe write plus one newline-delimited JSON response from the QEMU main loop.
4. **Post-Input Settle (`settle_after_input_ms`)**: The configured delay between submitting input and capturing the post-action screenshot (`120 ms` by default).
5. **Queue Wait**: Time spent queued behind another in-flight action on the same VM.

## 4. Physical Correctness & Endurance Criteria

These physical acceptance criteria require a real QEMU hypervisor and guest image and have no meaningful mock equivalent:

* **Click Coordinate Accuracy**: Clicking the four corners, center, and at least 200 grid points on `guest/fixtures/offline-workbench.html` lands within **≤ 2 native pixels**.
* **Mid-Session Resolution Change Protection**: Changing guest display resolution mid-session rejects subsequent actions built on pre-resize frames with `STALE_FRAME` rather than clicking the wrong coordinates.
* **20 Start/Stop/Reset Cycles**: Zero orphaned QEMU or supervisor processes, and the read-only base image SHA-256 digest remains unchanged byte-for-byte.
* **30-Minute / 1,000-Action Soak Test**: Mixed moves, clicks, drags, wheel scrolls, key chords, and text entry complete with zero stuck keys, zero descriptor/memory leaks, and bounded `.runtime/` disk usage.
