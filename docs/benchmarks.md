# Benchmarks

**English** | [简体中文](benchmarks.zh-CN.md)

Two things to be clear about before reading further.

**Nothing here has been measured yet.** The numbers below are the plan's targets, kept as
targets. This change adds no real capture, input or boot timing measurements for the
expanded platform matrix, and this page contains no benchmark results.

**A mock is not a measurement.** The test doubles in this repository (a fake QEMU process,
an in-process QMP server) are fast because they do no virtualisation, rendering or guest
scheduling at all. Timings taken against them say something about Python overhead and
nothing about whether a real desktop meets a latency target. Any report must label its
rows as `mock`, `TCG`, `WHPX`, `HVF` or `KVM`, including host and guest architectures.

## How to measure

```bash
PALETTE_E2E=1 PALETTE_E2E_PROFILE=tcg-x86_64 uv run pytest tests/e2e -v
```

Choose a configured profile for the guest ISA and accelerator being measured. Optional
QEMU/image/accelerator overrides are described in the [operator guide](operator.md).

On a fixed host — one VM, 1280×800, 2 vCPU, 4 GiB, no heavy load in the guest — record host
CPU, memory and disk alongside each figure.

## Targets (not results)

| Measurement | Target |
| --- | --- |
| "request → MCP image result ready", over 100 stable captures | P95 ≤ 500 ms |
| "action start → post-action image ready", simple click, model inference excluded | P95 ≤ 1 s |
| Cold start, split into: QMP reachable / first valid framebuffer / desktop ready | recorded separately, no single target |

Long text has its own rate limit and does not share the click's latency budget: typing 256
characters is hundreds of input messages, and pretending otherwise would set an impossible
goal and then quietly miss it.

## Where the time goes

When a target is missed, decompose before adding complexity:

1. **Image bytes and encoding** — Pillow's PNG encode is often the largest single cost.
2. **Temporary file I/O** — `screendump` writes to disk; the service reads it back and
   deletes it.
3. **QMP round trip** — a pipe write plus a JSON response.
4. **Guest settle** — the configured delay between input and capture.
5. **Queue wait** — time spent behind another action.

If the totals cannot be met and the bottleneck is capture I/O, the plan's answer is a VNC
capture backend, not a faster fake: QMP's whole-surface dump is not designed for high frame
rates, whereas VNC maintains a live framebuffer. Input would stay on QMP, so only the
capture path changes.

## Correctness measurements

Some acceptance criteria are physical and have no useful mock equivalent:

* click accuracy at a grid's corners, centre and at least 200 points, error ≤ 2 original
  pixels;
* a resolution change mid-session being refused rather than mis-clicked;
* twenty start/stop cycles with no orphaned processes and an unchanged base-image digest;
* 1000 mixed actions over at least 30 minutes, checking for stuck keys, leaks and disk
  growth.

A CI machine without hardware acceleration can run the unit suite and a TCG smoke test
against a trusted image. That is not evidence that WHPX/HVF/KVM or the isolation properties hold,
and the report must not present it as such.
