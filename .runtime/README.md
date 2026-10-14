# Palette Runtime Directory (`.runtime/`)

**English** | [简体中文](README.zh-CN.md)

Everything Palette creates, mutates, or cleans up at runtime lives inside this directory (configurable via `runtime_root` in `palette.toml`) and nowhere else:

```
.runtime/
├── .operator/                       # Local operator IPC discovery (endpoint.json) and single-owner lock (owner.lock)
├── definitions/<vm_id>/             # Persistent VM definitions, writable disks, and UEFI vars created by Palette Desktop
├── vms/<vm_id>/<instance_id>/       # Per-instance overlay.qcow2, transient frame scratch space, logs, and manifest.json
└── audit/palette.jsonl              # Rotating metadata-only audit log (action type, length, timing, outcome)
```

## Confinement & Cleanup Rules

* **`PathGuard` Boundary**: This directory is ignored by Git and serves as Palette's strict confinement boundary. Every managed path resolved by the service is verified to reside inside `runtime_root` before any read, write, or unlink occurs, and `PathGuard` refuses to follow symlinks, junctions, or reparse points that lead outside.
* **Manual Cleanup**: While no VM or runtime owner (`daemon` / `serve`) is running, deleting ephemeral instance directories under `vms/` is safe — the next start creates a fresh `overlay.qcow2` from the read-only base image (note that `definitions/` holds persistent VMs created in Palette Desktop). **Never delete this directory while a VM is running**, as the active QEMU process holds its overlay and pipes open.
