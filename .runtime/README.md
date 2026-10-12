# Runtime directory

**English** | [简体中文](README.zh-CN.md)

Everything Palette creates at run time lives here and nowhere else:

```
vms/<vm_id>/<instance_id>/    instance overlays, frame scratch space, logs, manifest
audit/palette.jsonl           bounded audit metadata
```

This directory is ignored by Git. It is also the confinement boundary: every path the
service resolves is checked to be inside it, before it is read, written or deleted, and the
service refuses to follow a junction or reparse point that would lead outside.

Deleting this directory by hand is safe while no VM is running — the next start creates a
fresh overlay from the read-only base. Do not delete it while a VM is running; the running
process holds its overlay open.

Override the location with `runtime_root` in `palette.toml`.
