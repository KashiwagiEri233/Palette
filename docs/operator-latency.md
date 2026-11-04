# Low-latency operator display

**English** | [简体中文](operator-latency.zh-CN.md)

The low-latency mode keeps noVNC display and policy-controlled QMP input. It enables QEMU's CPU D-Bus display backend alongside VNC, without starting the raw-frame helper. Agent observations retain authoritative QMP screenshots. Image encoding runs outside the serialized input/acquisition queue.

## Settings and compatibility

In **Settings → Runtime environment**, enable **Low-latency display** (the current Chinese UI labels are “设置 → 运行环境 → 低延迟显示”). The authenticated `display.settings.get/update` operator APIs persist the local choice. Changes apply through a VM shutdown and subsequent start, which rebuilds QEMU's display configuration. Settings never restart a running VM and show each active instance's actual display mode, fallback reason and whether it needs a stop/start.

An explicit compatibility choice restores the original VNC launch path and disables D-Bus and its helper, including a configured raw experiment. With no local preference, the existing trusted experiment configuration is preserved. Low-latency mode probes the actual QEMU binary for D-Bus support and otherwise uses compatibility display with a reported reason. Input credentials remain in the backend/main process. This preference is not an Agent-selected transport.

VNC refresh can back off when idle. QMP input does not enter VNC's input-message wake path. QEMU's global refresh scheduling takes the minimum display-listener interval. A D-Bus console supplies the default 30 ms interval. Displayed frame rate depends on guest and host processing, and idle CPU work can increase. [QEMU implementation](https://github.com/qemu/qemu/blob/master/ui/console.c)

On this Windows host, an isolated paused, diskless QEMU 11.1.0 test completed a read-only RFB handshake: `-display none` backed off to a 780 ms refresh interval over approximately six seconds, while `-display dbus,p2p=yes,gl=off` remained at 30 ms without a helper. The real-desktop dragging and scrolling acceptance status is tracked separately below.

## Screenshot ordering

Public observation acquires immutable screenshot bytes in the existing queue. It fixes native dimensions, display/lifecycle epochs, action sequence, acquisition time and an internal capture sequence before allowing subsequent input. PNG decoding, scaling and thumbnails then run outside that queue. Each VM reserves at most two snapshot slots before queueing, so slow encoders cannot create an unbounded byte backlog. Short file I/O does not wait on the image-encoding semaphore.

A completed observation retains its acquisition action sequence even if later input occurred. Existing action validation therefore rejects it when stale. Registry selection and eviction use acquisition order, so an older slow encoding cannot replace a newer action screenshot. Lifecycle or resolution changes fence publication. Cancellation and timeout fence late-frame publication. Closing releases held input before waiting for processing cleanup. Action-internal and post-action captures retain their existing transaction semantics.

## Reproduce the real desktop comparison

Use disposable guest copies, the same host, QEMU version, accelerator, CPU/RAM, image version and capture configuration. The harness sends Space, drag and wheel events; it does not start, stop or edit VM definitions. Run the fixture **inside the guest browser**, fullscreen and focused: `guest/fixtures/display-latency.html`. Set the guest's actual resolution first. Use 100% browser zoom and device pixel ratio 1 so CSS marker pixels match native framebuffer pixels. Chromium/Edge supports `--force-device-scale-factor=1` at startup. All five binary markers must be visible at native coordinates. Use `--marker-x/--marker-y` to specify any framebuffer offset. The harness waits for valid painted markers before acquisition or measurement. Readiness requires valid pixels in the negotiated canvas.

Keep the disposable guest awake for the whole matrix: disable display blanking/DPMS, screensaver, automatic locking and standby, with identical power settings for both variants. For Windows, run `powercfg /change monitor-timeout-ac 0` and `powercfg /change standby-timeout-ac 0` **inside the disposable guest**, alongside its screensaver/lock settings. If the guest is already blank, restore it during preparation through the normal lease-controlled input path and verify the fixture again before measurement. A black QMP screenshot can indicate guest DPMS rather than a renderer failure.

After markers are ready and control is acquired, the harness clicks the fixture's blank center with a 40 ms button hold to focus its browser, then sends one Space and verifies its numbered feedback. This one-time calibration is recorded under `preparation` with `measured: false`. It precedes resource sampling. Each load and round resets its timing arrays. Calibration failure stops the run and records diagnostics rather than replaying uncertain input. This also avoids measuring focus left on a startup or power-management command window.

The benchmark runs against a built Desktop bundle.

```sh
npm --prefix desktop run build
```

For a comparison of the complete optimization, run the isolated owner below in a separate terminal and wait for its `ready: true` line. The root must already contain a disposable configuration and profiles, with `runtime_root` beneath it and no live owner. The command establishes the isolated benchmark backend.

```sh
uv run python scripts/operator_latency_owner.py --root /absolute/test-root --disposable --observation-pipeline serial
```

`serial` hides the optional split-capture interface in this process only, reproducing the old acquisition-plus-encoding FIFO transaction. There is no production configuration flag. Select compatibility display and start a disposable VM through Desktop connected to that test root. Use the actual VM ID and immutable guest-image version at each resolution.

```sh
node desktop/scripts/benchmark-display.mjs --root /absolute/test-root --vm-id VM_ID --provider rfb --variant adaptive --width 1280 --height 800 --guest IMAGE_VERSION --guest-arch x86_64 --accelerator whpx --output .runtime/adaptive-800.json
node desktop/scripts/benchmark-display.mjs --root /absolute/test-root --vm-id VM_ID --provider rfb --variant adaptive --width 1920 --height 1080 --guest IMAGE_VERSION --guest-arch x86_64 --accelerator whpx --output .runtime/adaptive-1080.json
```

After the baseline, stop the disposable VM and then stop its isolated owner through the authenticated operator endpoint.

```sh
uv run python -c "import asyncio; from pathlib import Path; from palette.config import load_config; from palette.operator import OperatorClient; asyncio.run(OperatorClient(load_config(Path('/absolute/test-root')).runtime_root).call('runtime.stop'))"
```

The candidate owner uses the production split pipeline.

```sh
uv run python scripts/operator_latency_owner.py --root /absolute/test-root --disposable --observation-pipeline split
```

Enable low-latency display and start only the disposable VM. Repeat with `--variant steady` and output names `steady-800.json` / `steady-1080.json`. The harness checks the actual instance mode and pipeline. It verifies that the running instance has applied the requested mode. Use the same explicit VM stop, authenticated owner stop and owner start when preparing the next resolution or root. This sequence applies both the owner pipeline and QEMU display arguments.

```sh
uv run python scripts/compare_display_reports.py --baseline .runtime/adaptive-800.json .runtime/adaptive-1080.json --candidate .runtime/steady-800.json .runtime/steady-1080.json --output .runtime/operator-latency-comparison.json
```

Defaults are three rounds, 100 numbered key inputs and 100 paced inputs for each motion phase at 60 Hz, under no-observation (`idle`) and single-concurrency authoritative-observation (`observe`) loads. `idle` names the background load. Separate static intervals of **10 and 120 seconds** measure first feedback. Across the three rounds, those wake inputs cover key, drag and wheel. Agent observation continues during static intervals in the `observe` load. Default static waits total about 13 minutes per report, or 52 minutes for four reports, excluding input/encoding work.

`--samples`, `--rounds`, `--motion-hz`, `--idle-seconds` and `--wake-samples` support exploratory runs. For example, `--samples 5 --rounds 1 --idle-seconds 1` is a smoke run for diagnostics. Acceptance uses the complete default protocol. Do not shorten the final protocol to obtain a pass.

The report records actual renderer DOM event times and corresponding guest markers, matched motion latency, frame intervals, stale-target lag and final-target settling. Four-pixel coordinate buckets tolerate pointer quantization. Repeated drag endpoints also require a new guest motion marker, so a previous round cannot satisfy a new input. Intermediate moves merged by the input queue or never presented are counted separately from lost final feedback. Wheel calibration relates host detents to cumulative guest displacement, so coalesced guest events do not corrupt counts. ACK diagnostics identify batches with sequence, event count and renderer timestamps. Their queue timestamp starts the queue window and can predate a retained move after coalescing. It is not each individual DOM input's time. CPU/RSS cover backend/QEMU/helper plus Electron processes. CPU uses cumulative process CPU seconds divided by monotonic elapsed seconds and multiplied by 100. One fully occupied core is 100%, and a process group can exceed 100%. RSS is summed working-set bytes and can count shared pages more than once. Static resource intervals are separate from active measurements. Canvas readback measures client visibility.

## Acceptance and evidence

The comparator requires both resolutions and loads, at least three distinct rounds, at least 100 inputs, matched final motion targets, valid process-resource coverage and complete 10/120-second idle evidence. In 1080p continuous-observation load, **both drag and scroll input-to-visible P95 must improve by at least 20%**. Other measured delays, frame intervals, settling, active CPU and RSS must not regress by more than 10%. Idle CPU/RSS increases are reported separately. Missing or mismatched evidence fails closed.

The complete comparison is **adaptive + serial → steady + split**, reported as `comparison_scope: display_and_observation` and, on success, `combined_optimization_gate_passed: true`. Comparing split → split is also supported, but reports `display_refresh_only` and cannot establish the benefit of moving encoding out of the FIFO. Observation pipeline is a variant field, separate from stable host/guest execution metadata. Legacy raw reports remain readable with their original evidence limitations.

Settings persistence, unsupported-build fallback, resize, ownership change, disconnect, outside-canvas release and lifecycle cancellation require correctness tests in addition to the performance comparison. Raw-frame reports remain supported with `--provider raw --variant raw`. That experiment retains its separate migration gate and never authorizes Agent actions from shadow frames.

| Platform | Refresh mechanism | Full real-desktop matrix |
| --- | --- | --- |
| Windows x86_64 | Paused diskless QEMU trace verified | Not accepted until complete reports pass |
| Linux | Not verified locally | Not accepted |
| macOS | Not verified locally | Not accepted |

Firmware, mocked IPC and unit tests establish specific behavior only. None replaces the full desktop matrix. Local low-latency enablement follows acceptance. VNC supplies the display, and raw Canvas requires explicit experimental configuration. Raw local reports belong under `.runtime/` and must not be committed.

## Local evaluation on 2026-11-04

**Decision — keep low-latency display opt-in. Windows desktop performance remains unaccepted.** The user chose delivery with an unaccepted report after native Electron focus changes repeatedly interrupted long-idle testing. The original local preference remains inherited. The disposable VM and its benchmark owner were stopped.

The desktop probe used an isolated Windows 11 build 22000.2777 copy with QEMU 11.1.0, WHPX, q35, virtio-vga, 2 vCPUs and 3072 MiB RAM. Observations used the existing 1280-pixel long-edge limit. This resource configuration belongs to the disposable test profile. Original VM storage was used as a read-only backing image.

The adaptive/serial 1920×1080 smoke completed one round per load with 20 numbered key inputs and 20 dispatched inputs per motion phase. It used one-second idle intervals. These small-sample values describe that baseline smoke run only.

| Load | Key feedback P95, ms | Matched drag / issued | Drag feedback P95, ms | Matched scroll / issued | Scroll feedback P95, ms |
| --- | --- | --- | --- | --- | --- |
| No observation | 157.0 | 15 / 20 | 93.0 | 12 / 20 | 111.1 |
| Continuous observation | 380.1 | 2 / 20 | 374.8 | 3 / 20 | 297.2 |

The subsequent 100-input baseline attempts stopped during the long-idle protocol. Native diagnostics recorded window blur, focus and minimization events, followed by the existing automatic control release. An additional diagnostic stopped with unmatched final drag feedback. Failed runs retain their original errors and screenshots. Input with an uncertain outcome was never replayed. The fixture now disables native selection and touch panning during its drag target, and the harness checks ownership before keys and requires fresh motion evidence for repeated endpoints.

The stable/split candidate and the 1280×800 matrix were not measured. A 20% improvement, the 10% CPU/RSS limits and idle resource increases therefore remain undetermined. The reproducible harness retains the three-round, 100-input and 10/120-second acceptance protocol. Keep its native window in the foreground throughout a run. It temporarily prevents host display sleep and records focus changes. Window focus and release policies remain active during measurement.

Local evidence is under `.runtime/latency-acceptance-20261030/`. `evaluation-20261104.json` indexes the raw reports with hashes, including `adaptive-smoke-v9.json`, `adaptive-1080-final.json`, `adaptive-1080-v2.json` and `idle-control-diagnostic-v3.json`. The directory name preserves the original experiment date.

| Coverage | Windows | Linux / macOS |
| --- | --- | --- |
| Desktop build | Passed | Native builds unverified |
| Backend and simulated branches | 143 focused tests passed on 2026-11-04 | Platform launch branches covered by host-side simulation |
| Frontend | 194 tests and settings Electron flow passed | Native Electron execution unverified |
| Real QEMU | Refresh mechanism passed, baseline desktop smoke completed | Unverified |
| Full desktop acceptance | Unaccepted | Unaccepted |

Ruff, formatting and strict mypy also passed. The earlier backend full run on 2026-10-30 had 1059 passing and 32 skipped tests. These checks establish implementation and ordering coverage. The complete real-desktop performance gate remains open.
