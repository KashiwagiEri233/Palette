# Guest images

**English** | [简体中文](README.zh-CN.md)

Put the read-only desktop template here, for example
`debian-13-xfce-desktop.qcow2`, and point a profile at it:

```toml
[images]
base = "G:/Workspace/Palette/images/debian-13-xfce-desktop.qcow2"
format = "qcow2"
sha256 = "<digest>"
```

The files in this directory are ignored by Git: an image is large, host-specific and
usually carries a distribution's licence obligations. What *is* committed is the profile's
record of the image — its path, its format and its SHA-256 — which is what turns "whatever
file is at that path" into "the image this profile is allowed to run".

Build instructions are in `../docs/guest-template.md`. Run `uv run palette doctor` after
placing an image: it verifies the extension against the declared format and checks the
digest when one is pinned.

Nothing here is ever modified. Each run gets its own writable qcow2 overlay under
`.runtime/`, and a reset discards that overlay rather than touching the base.
