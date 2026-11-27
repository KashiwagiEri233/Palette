# Experimental Desktop display

**English** | [简体中文](display-experiment.zh-CN.md)

The experiment shares a host-owned QEMU CPU framebuffer between Desktop and shadow PNG capture. It is **off by default**. VNC remains the production default, and all authoritative Agent frames, coordinate checks and post-action screenshots still use QMP. This stage recommends retaining VNC until the complete real-desktop matrix passes.

## Build and enable

The helper in `native/display-bridge/` needs a C11 compiler, GLib/GIO 2.66+, Meson and Ninja. Linux also needs the `gio-unix-2.0` development package. QEMU must list `dbus` in `qemu-system-* -display help`.

```sh
meson setup .runtime/display-build native/display-bridge
meson compile -C .runtime/display-build
```

On Debian/Ubuntu, install `build-essential libglib2.0-dev meson ninja-build pkg-config`. On macOS, use a native Homebrew environment with `glib meson ninja pkgconf`. On Windows, build in an MSYS2 UCRT64 shell with `mingw-w64-ucrt-x86_64-gcc`, `mingw-w64-ucrt-x86_64-glib2`, `mingw-w64-ucrt-x86_64-meson`, `mingw-w64-ucrt-x86_64-ninja` and `mingw-w64-ucrt-x86_64-pkgconf`. Keep the matching GLib runtime DLL directory on the backend's PATH, or package its runtime DLL dependencies beside the helper. These dependencies are local build prerequisites, not new guest requirements.

Add these **trusted host settings** to `palette.toml`, substituting an absolute helper path:

```toml
[host]
experimental_display = "dbus"
experimental_display_helper = "G:/Workspace/Palette/.runtime/display-build/palette-display-bridge.exe"
```

Stop disposable test VMs before restarting the backend to load settings. The change applies to newly started Desktop VMs. Setting `experimental_display = "off"` restores the default launch path after restarting the backend and VM. The separate [operator display setting](operator-latency.md) overrides this experiment: an explicit compatibility choice disables D-Bus/helper, while low-latency mode uses D-Bus with VNC and no helper. With no local preference, the trusted experiment configuration remains effective. No guest disk conversion or Agent-selected transport is introduced.

Missing helper/build capability falls back to VNC. A helper failure disconnects raw viewers and releases their control; reconnecting explicitly starts a new helper and source generation. During a video mode switch, viewers disconnect while a complete surface is obtained; reconnect the display. Unknown pixels are never padded into an apparently valid frame.

## Architecture and interfaces

- A supervised C/GIO helper subscribes to `org.qemu.Display1.Listener` CPU `Scanout`/`Update`, cursor and geometry events. It advertises no Map/GPU interfaces. Current pixel support is 32-bit RGB/BGR; unsupported formats fall back to VNC.
- The runtime alone passes sockets and issues QMP. Windows uses `get-win32-socket`; experimental POSIX launches replace the sole stdio QMP with a private AF_UNIX connection supporting `getfd`/SCM_RIGHTS. Normal POSIX launches retain stdio. No second management channel is added.
- `FrameSource` validates bounds, composes updates and publishes immutable RGBA images. Each viewer has one pending frame and receives complete full frames initially or when coalescing skips sequences, and incremental dirty-rect or zero-pixel cursor packets when sequences are contiguous. An eight-byte big-endian sequence ACK permits the next frame; arbitrary client messages are rejected. Slow rendering cannot accumulate an unbounded queue of full frames. VNC's incremental bytes are never dropped.
- `display.ticket` adds `provider` (`rfb`/`raw`), `generation` and `instance_id`. Older results without `provider` remain VNC. Existing single-use ticket, origin checks and private VNC credentials remain enforced.
- Operator-only `display.shadow` requires `instance_id` and returns encoding time, sizes, source sequence/age and `authoritative: false`. It returns no `frame_id`, cannot authorize actions and is not an Agent tool. `experimental-display-v1` advertises these diagnostics.
- `control.metrics.resources` adds per-process cumulative CPU seconds and RSS for the backend process tree, plus an incomplete flag. `execution` records actual QEMU version, CPU/RAM, machine/display model and capture limits so differing configurations cannot pass the comparison. RSS sums can count shared pages more than once; compare the same sampling method. Input leases, epochs, ordering and unknown-result replay rules are unchanged.

The helper follows the [upstream QEMU test](https://github.com/qemu/qemu/blob/master/tests/qtest/dbus-display-test.c) and [D-Bus contract](https://www.qemu.org/docs/master/interop/dbus-display.html). This CPU experiment makes no GPU zero-copy or remote-streaming claim.

## Reproduce validation

Run the application tests and explicit real-QEMU firmware check:

```sh
uv run python -m pytest tests/unit/test_frame_source.py tests/integration/test_raw_display.py
npm --prefix desktop run build
npm --prefix desktop test
```

```powershell
$env:PALETTE_DBUS_QEMU = 'C:\Program Files\qemu\qemu-system-x86_64.exe'
$env:PALETTE_DBUS_HELPER = 'G:\Workspace\Palette\.runtime\display-build\palette-display-bridge.exe'
$env:PALETTE_DBUS_ACCEL = 'whpx' # or explicitly tcg
uv run python -m pytest tests/e2e/test_dbus_display.py --basetemp=.runtime/dbus-acceptance
cd desktop
npm exec -- playwright test e2e/raw-display.spec.ts e2e/human-input.spec.ts
```

The firmware test verifies exact pixels against QMP, startup mode changes, helper crash/restart, held-input release and base-image preservation. The Electron test checks raw canvas rendering and drag release outside the canvas. A halted firmware guest cannot establish application input feedback or desktop performance.

For an observation-only microbenchmark (three rounds, 100 samples each):

```powershell
uv run python scripts/display_probe.py --firmware --accelerator whpx --qemu $env:PALETTE_DBUS_QEMU --helper $env:PALETTE_DBUS_HELPER --output .runtime/display-observation.json
```

Without `--firmware`, supply `--root` and `--profile` for a configured disposable guest image. This probe creates its own isolated runtime, reports actual resolution and deliberately leaves input-to-visible and renderer metrics null. Both capture paths are active, so its resources are not a production A/B comparison.

For full Desktop measurements, prepare two dedicated test roots with the same guest image and resources: default VNC and experimental D-Bus. Start each disposable VM and open `guest/fixtures/display-latency.html` **inside the guest browser**, fullscreen and focused. Set actual guest resolutions to 1280×800 and 1920×1080. Do not use a production guest: the harness sends Space, drag and wheel events.

Run from the repository root, once per provider and resolution:

```sh
node desktop/scripts/benchmark-display.mjs --root /absolute/test-root --vm-id VM_ID --provider raw --width 1920 --height 1080 --guest IMAGE_VERSION --guest-arch x86_64 --accelerator whpx --output .runtime/raw-1080.json
```

Use `--provider rfb --variant adaptive` for the baseline and `--provider raw --variant raw` for this experiment. Optional `--marker-x`/`--marker-y` specify native framebuffer coordinates if the guest browser fullscreen surface has an offset. Defaults are 3 rounds × 100 numbered inputs under no-observation and single-concurrency continuous authoritative-observation loads, with paced, coordinate/displacement-matched drag/scroll feedback and separate 10/120-second static wake measurements. The current fixture has five markers. See the [operator benchmark protocol](operator-latency.md) for exact measurements, runtime and smoke options. Shadow encoding is sampled outside the input/resource window. Client canvas visibility is not physical monitor presentation. Artifacts contain timings and metadata, not screenshots or typed guest content.

```sh
uv run python scripts/compare_display_reports.py --baseline .runtime/rfb-800.json .runtime/rfb-1080.json --candidate .runtime/raw-800.json .runtime/raw-1080.json --output .runtime/display-comparison.json
```

Incomplete/mismatched matrices cannot pass. The raw experiment gate requires ≥20% improvement in 1080p loaded feedback or shadow observation P95, with no >10% regression in feedback, input ACK, authoritative observation, motion frame intervals, matched motion latency, settling, wake feedback, active total CPU or RSS. It checks both resolutions/loads, three distinct rounds and sample counts. Process membership changes invalidate resource comparisons. Legacy raw reports retain their original measurements and cannot establish steady-refresh acceptance. Passing only qualifies further migration review: shadow frames still need a separate correctness design before replacing authoritative capture.

## Recorded evidence, 2026-10-30

Windows x86_64, QEMU 11.1.0 (`v11.1.0-12130-ge470268ff4`), WHPX and TCG firmware acceptance passed. Local C build used GLib 2.90.1, Meson 1.12.1 and Zig 0.15.2; C compilation was warning-clean. Zig's linker required local `-Dwerror=false` because it rejects Meson's `--fatal-warnings` switch. No prebuilt helper or third-party DLL is committed.

Observation-only WHPX, **actual 720×400 halted firmware**, alternating sample order:

| Round | Samples per path | QMP observe P95 | Shadow PNG P95 |
| --- | --- | --- | --- |
| 1 | 100 | 54.255 ms | 6.209 ms |
| 2 | 100 | 55.937 ms | 5.619 ms |
| 3 | 100 | 58.696 ms | 5.904 ms |

Raw local samples are saved in `.runtime/display-whpx-observation.json`. These figures do **not** establish 1080p desktop feedback improvement: the authoritative path includes frame registration, shadow capture has weaker semantics, no renderer was sampled and both providers were active. The configured 1280×800 profile did not describe the actual firmware resolution; the report uses measured dimensions.

| Host | Native build | Real QEMU firmware | Full desktop performance matrix |
| --- | --- | --- | --- |
| Windows x86_64 | Verified | WHPX + TCG verified | Not accepted |
| Linux | Not verified locally | Not verified | Not accepted |
| macOS | Not verified locally | Not verified | Not accepted |

**Decision: retain VNC as default.** The shared CPU source is feasible on this Windows host, and shadow capture is promising. Full guest-desktop latency, resource, cross-platform and endurance evidence is still required for a production migration.
