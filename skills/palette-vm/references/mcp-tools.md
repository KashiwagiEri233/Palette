# Palette MCP tools and recovery

Read this for operation-specific parameters. The connected server's advertised schema is authoritative; optional tools depend on profile configuration and live guest readiness. Prefixes added by a client are not part of the names below.

## Baseline

| Tool | Input and meaning |
| --- | --- |
| `vm_list` | No arguments. Lists known persistent/runtime VMs without starting them or acquiring control. |
| `vm_status` | `vm_id`. Lifecycle, readiness, control state and live capabilities; also reports held inputs. |
| `vm_observe` | `vm_id`, optional `max_long_edge`. Returns PNG image blocks and matching `frame` metadata. |
| `vm_act` | `vm_id`, `instance_id`, `frame_id`, `request_id`, `action`. Performs one bounded action and normally returns a receipt and new screenshot. |
| `vm_wait` | `vm_id`, optional `after_frame_id`, `mode`, `timeout_ms`, `delay_ms`, `threshold`. `changed` and `stable` need a baseline frame; the maximum wait is 10 seconds. |
| `vm_release_inputs` | `vm_id`, `instance_id`. Releases held keys/buttons; check `release_confirmed` rather than assuming release succeeded. |

`frame` contains `frame_id`, `vm_id`, `instance_id`, `boot_epoch`, `controller_epoch`, `display_epoch`, `action_seq`, `native_size` and `image_size`. Coordinates are in the delivered PNG's `image_size`; the service handles conversion to the native framebuffer. Old frames are fenced after changes in state, ownership, geometry or boot generation, and also expire with time.

### A single visual action

This is a template, not a runnable request. Replace uppercase values with actual returned IDs and a fresh request ID. Derive the illustrative coordinates from the current screenshot.

```json
{
  "vm_id": "RETURNED_VM_ID",
  "instance_id": "RETURNED_INSTANCE_ID",
  "frame_id": "RETURNED_FRAME_ID",
  "request_id": "GENERATE_FRESH_UUID",
  "action": { "type": "click", "x": 120, "y": 80, "button": "left", "count": 1 }
}
```

| `action.type` | Fields and limits |
| --- | --- |
| `move` | `x`, `y`. |
| `click` | `x`, `y`, optional `button` (`left`, `middle`, `right`) and `count` (`1` or `2`). |
| `scroll` | `x`, `y`, `direction` (`up`, `down`), `ticks` (`1`-`20`). Vertical only. |
| `key` | `keys`, optional `hold_ms` (`10`-`500`). Names/chords, for example `CTRL` + `V`, `ENTER`, `TAB`. |
| `type_text` | `text`, `mode: "keyboard"`. Printable US ASCII; no newlines, tabs, CJK or emoji. |
| `drag` | `points: [{"x":...,"y":...}, ...]` (2-64 points), optional `button` and `duration_ms` (0-3000). |

All coordinates must be inside `image_size`; unknown action fields are rejected. Read `structuredContent` when available, otherwise parse the matching text result; preserve the accompanying image blocks for visual inspection.

## Optional guest tools

These tools use `vm_id`, `instance_id`, `boot_epoch` and `request_id` unless their advertised schema says otherwise. Copy identity from current status/frame information; a tool being registered does not prove its guest channel is ready.

| Tool | Useful details |
| --- | --- |
| `vm_guest_input_text` | Adds `frame_id`, `text`; `method` defaults to `clipboard_paste`, `target` to `focused`. Sets the guest clipboard and pastes atomically. `native_text` needs the separate guest service. |
| `vm_guest_clipboard_get`, `vm_guest_clipboard_set` | Guest clipboard only. Set adds `text`, up to 48 KiB UTF-8; it does not paste. Use atomic input when ordinary paste is appropriate. |
| `vm_guest_filesystem` | Guest paths, `mode`: `read`, `write`, `copy`, `move`, `delete`, `list`, `search` or `info`. Text read `offset` is one-based; `limit` bounds output. Relative paths begin at the guest Desktop. |
| `vm_guest_powershell`, `vm_guest_process`, `vm_guest_registry` | Operations run inside the guest user's security context. Read their schemas and current capability results before choosing an operation. |
| `vm_guest_app` | `mode`: `launch`, `launch_executable`, `resize` or `switch`. Executable, arguments and working directory are guest values; verify the resulting window. |
| `vm_guest_launch_app` | The separate allowlisted launcher accepts `app_id` and optional `options`, not an arbitrary executable. Do not confuse it with `vm_guest_app`. |
| `vm_guest_snapshot` | UI Automation snapshot plus screenshot; returns `snapshot_id`, labels, window IDs and the corresponding frame. May be truncated. |
| `vm_guest_multi_select` | Adds `frame_id` and exactly one of `locs=[[x,y],...]` or `labels=[id,...]`; labels also need `snapshot_id`. At most 64 targets; `press_ctrl` defaults to true. |
| `vm_guest_multi_edit` | Adds `frame_id` and exactly one of `locs=[[x,y,text],...]` or `labels=[[id,text],...]`; labels also need `snapshot_id`. At most 64 fields; `clear` defaults to true. |
| `vm_guest_resize`, `vm_guest_sync_time` | Requests a guest display size or time synchronization when supported. Reobserve after display changes. |

If a terminal needs a different paste chord, use guest clipboard set plus an explicit key action supported by that application. Do not access the host clipboard. If no Unicode route is available, report that capability gap rather than altering the requested text.

### Binary file transfer

- `vm_guest_file_open` adds an absolute guest `path` and `mode` (`r` or `w`), returning `data.transfer_id`. Opening in `w` overwrites the guest file; ensure that is within the requested task.
- `vm_guest_file_read` adds `transfer_id` and optional `count`; returns `data.data_base64`, `data.count`, `data.eof`. At most 49152 decoded bytes per chunk.
- `vm_guest_file_write` adds `transfer_id` and `data_base64`; at most 49152 decoded bytes per chunk. Check the actual accepted count.
- `vm_guest_file_close` adds `transfer_id`. Close on completion and attempt cleanup after an error when the handle is still valid. Reboots and handovers may invalidate handles; cancelled writes can leave partial files.

Use a new request ID for each new chunk; an intentional retry of the same logical chunk must retain its ID and exact payload within the supported deduplication context. Never restart an uncertain transfer or replay an unknown write without checking the resulting state.

## Lifecycle boundaries

| Tool | Meaning |
| --- | --- |
| `vm_start` | `vm_id`, optional `profile_id`. Advanced host-material fields (`image`, `image_format`, `iso`, `extra_disks`, `qemu_args`) have separate policy switches; do not invent host paths or use raw arguments to work around a denied operation. |
| `vm_stop` | `vm_id`, optional `force` (default false). Force stop is a separate destructive capability. |
| `vm_reset` | `vm_id`, optional `profile_id`. Restores initial system state by discarding managed system-disk modifications; it is not the normal restart action. |

Tool presence is not authorization. Respect both the user's requested scope and backend policy. Do not change profiles or stop/reset a VM merely to recover an application-level failure. A skill import does not grant these operations.

## Error decisions

| Result | Next step |
| --- | --- |
| `VM_NOT_FOUND` / `VM_NOT_RUNNING` | Recheck the selected VM. Start only when appropriate and authorized; never operate a substitute VM silently. |
| `STALE_FRAME` / `FRAME_NOT_FOUND` / `INVALID_COORDINATES` | Reobserve and recompute against the delivered image. A new logical action uses a new request ID. |
| `BUSY` / `RATE_LIMITED` / `CONTROL_NOT_OWNED` | Inspect control state, respect human/driver ownership and use bounded waiting. Do not force control or loop indefinitely. |
| `ACTION_OUTCOME_UNKNOWN` / `CAPTURE_FAILED_AFTER_INPUT` / `effect_state=unknown` | Input may have happened. Observe or read back before deciding; unresolved outcomes stay unresolved. |
| Timeout or transport loss after submission | Do not assume no effect. Reconnect/read state without blindly reissuing the action, even when a generic error says retryable. |
| `POLICY_DENIED` / `PATH_DENIED` / `OPERATOR_REQUIRED` | Report the specific restriction. Use an authorized alternative or wait for the user/operator; do not change trusted policy. |
| `UNSUPPORTED_TEXT` / `GUEST_TOOL_UNAVAILABLE` / `GUEST_TOOL_VERSION_MISMATCH` | Check live capabilities. Use an available route without changing the user's text or claiming unsupported work was done. |
| Batch result with `completed < total` or an unknown effect | Inspect which operations applied before deciding on remaining work. No whole-batch replay or automatic rollback. |

Neither `retryable=true` nor a new request ID makes an unknown effect safe to replay. Readback and fresh visual evidence determine the next action.
