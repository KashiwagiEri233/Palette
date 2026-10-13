"""Base images, per-instance overlays, manifests and retention.

The base image is the only durable artefact. Every run gets its own writable qcow2
overlay with an explicitly declared backing format and path, and the base is never
committed to: "reset" means *throw the overlay away and make a new one*, which is a much
stronger statement than an in-guest reboot.

Deletion only ever touches objects that (a) appear in a manifest and (b) resolve inside
the managed runtime root, so a mistaken identifier cannot turn into a recursive delete.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import HostConfig, Profile
from ..errors import ErrorCode, PaletteError
from ..policy import PathGuard, validate_identifier
from .launcher import locate_binary, run_sync, run_tool

logger = logging.getLogger("palette.vm.images")

#: Reading a multi-gigabyte base image to hash it is expensive; do it in chunks.
_HASH_CHUNK = 4 * 1024 * 1024


@dataclass(slots=True)
class InstancePaths:
    """Every host location belonging to one QEMU process instance."""

    vm_id: str
    instance_id: str
    root: Path
    disk_dir: Path
    overlay: Path
    frames: Path
    logs: Path
    manifest: Path
    hardware_vars: Path | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "overlay": str(self.overlay),
            "frames": str(self.frames),
            "logs": str(self.logs),
            "manifest": str(self.manifest),
        }


@dataclass(frozen=True, slots=True)
class ImageInfo:
    format: str
    chain: tuple[Path, ...]


def sha256_file(path: Path, *, chunk_size: int = _HASH_CHUNK) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def resolve_qemu_img(profile: Profile) -> str | None:
    """Find ``qemu-img``: the profile's explicit setting, then a sibling of the emulator,
    then ``PATH``."""
    if profile.qemu.qemu_img:
        explicit = Path(profile.qemu.qemu_img)
        if explicit.is_file():
            return str(explicit)
        found = shutil.which(profile.qemu.qemu_img)
        if found:
            return found
        return None
    candidates: list[Path] = []
    binary = Path(profile.qemu.binary)
    if binary.parent.is_dir():
        candidates.append(binary.parent)
    located = locate_binary(profile, HostConfig())
    if located and Path(located).parent.is_dir():
        candidates.append(Path(located).parent)
    for directory in candidates:
        for name in ("qemu-img", "qemu-img.exe"):
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    return shutil.which("qemu-img") or shutil.which("qemu-img.exe")


def validate_caller_path(path: Path | str, *, what: str = "file") -> Path:
    """Validate a caller-supplied host path: an image, a CD-ROM, an extra disk.

    These are the only paths Palette ever touches that are deliberately *outside* the
    managed runtime root, so this is a shape check rather than confinement: absolute,
    existing, and a regular file. A link is resolved, so the manifest and the audit trail
    record the file that was actually opened rather than the name that pointed at it.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        raise PaletteError(
            ErrorCode.PATH_DENIED,
            f"a caller-supplied {what} path must be absolute",
            details={"value": str(path)},
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PaletteError(
            ErrorCode.IMAGE_MISSING,
            f"the caller-supplied {what} does not exist",
            details={"value": str(path)},
        ) from exc
    if not resolved.is_file():
        raise PaletteError(
            ErrorCode.IMAGE_MISSING,
            f"the caller-supplied {what} is not a regular file",
            details={"value": str(path)},
        )
    return resolved


def _validate_caller_image(path: Path) -> Path:
    return validate_caller_path(path, what="image")


class ImageManager:
    """Owns everything under ``<runtime>/vms/<vm_id>/``."""

    def __init__(
        self,
        guard: PathGuard,
        profile: Profile,
        *,
        qemu_img: str | None = None,
        max_overlay_bytes: int | None = None,
        max_overlays: int = 8,
        keep_overlays: bool = True,
    ) -> None:
        self.guard = guard
        self.profile = profile
        self.qemu_img = qemu_img or resolve_qemu_img(profile)
        #: The full command prefix for the helper, so a wrapper or an alternate
        #: interpreter can stand in for the real qemu-img.
        self.qemu_img_argv: list[str] = (
            [self.qemu_img, *profile.qemu.qemu_img_prefix] if self.qemu_img else []
        )
        self.max_overlay_bytes = max_overlay_bytes
        self.max_overlays = max_overlays
        self.keep_overlays = keep_overlays

    # -- base image ------------------------------------------------------------------
    def verify_base_image(self, *, compute_hash: bool = False) -> dict[str, Any]:
        """Check the configured base image exists and matches its declared format.

        Hashing is opt-in because it reads the whole file; ``doctor`` does it on request
        and the launcher does it only when the profile pins a digest.
        """
        base = Path(self.profile.images.base)
        info: dict[str, Any] = {
            "configured": str(base),
            "exists": base.is_file(),
            "format": self.profile.images.format,
            "sha256_declared": self.profile.images.sha256,
        }
        if not base.is_file():
            info["ok"] = False
            info["reason"] = "missing"
            return info
        if not self.qemu_img_argv:
            info["ok"] = False
            info["reason"] = "qemu-img is required to verify the base image format"
            return info
        try:
            result = run_sync(
                [*self.qemu_img_argv, "info", "--output=json", str(base)], timeout=60.0
            )
            actual: Any = json.loads(result.stdout or "{}")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            info["ok"] = False
            info["reason"] = "qemu-img could not inspect the base image"
            return info
        if result.returncode != 0 or not isinstance(actual, dict):
            info["ok"] = False
            info["reason"] = "qemu-img could not inspect the base image"
            return info
        info["actual_format"] = actual.get("format")
        if actual.get("format") != self.profile.images.format:
            info["ok"] = False
            info["reason"] = "actual disk format does not match the profile"
            return info
        info["size_bytes"] = base.stat().st_size
        info["ok"] = True
        if compute_hash or self.profile.images.sha256:
            digest = sha256_file(base)
            info["sha256"] = digest
            declared = self.profile.images.sha256
            if declared and declared.lower() != digest.lower():
                info["ok"] = False
                info["reason"] = "sha256 mismatch against the trusted profile"
        return info

    def require_base_image(self) -> Path:
        base = Path(self.profile.images.base)
        if not base.is_file():
            raise PaletteError(
                ErrorCode.IMAGE_MISSING,
                "the profile's base image is not present on this host",
                details={"profile": self.profile.profile_id},
            )
        declared = self.profile.images.sha256
        if declared:
            digest = sha256_file(base)
            if digest.lower() != declared.lower():
                raise PaletteError(
                    ErrorCode.IMAGE_INVALID,
                    "the base image does not match the digest pinned in the profile",
                    details={"profile": self.profile.profile_id},
                )
        return base

    # -- instance layout -------------------------------------------------------------
    def allocate_instance(self, vm_id: str, instance_id: str) -> InstancePaths:
        validate_identifier(vm_id, what="vm_id")
        validate_identifier(instance_id, what="instance_id")
        root = self.guard.resolve("vms", vm_id, instance_id)
        disk_dir = root / "disk"
        frames = root / "frames"
        logs = root / "logs"
        root.mkdir(parents=True, exist_ok=False)
        hardware_vars: Path | None = None
        if self.profile.qemu.firmware.vars:
            hardware_vars = disk_dir / "vars.fd"
        paths = InstancePaths(
            vm_id=vm_id,
            instance_id=instance_id,
            root=root,
            disk_dir=disk_dir,
            overlay=disk_dir / "overlay.qcow2",
            frames=frames,
            logs=logs,
            manifest=root / "manifest.json",
            hardware_vars=hardware_vars,
        )
        try:
            self.write_manifest(paths, {"state": "STARTING", "process_exited": False})
            for directory in (disk_dir, frames, logs):
                directory.mkdir()
        except Exception:
            if self.read_manifest(vm_id, instance_id) is not None:
                self.delete_instance(vm_id, instance_id)
            else:
                # A failed manifest write never authorizes recursive deletion.
                with contextlib.suppress(OSError):
                    root.rmdir()
            raise
        return paths

    def stage_firmware_vars(self, paths: InstancePaths) -> None:
        """Copy the read-only variable template into the instance's writable store."""
        template = self.profile.qemu.firmware.vars
        if not template or paths.hardware_vars is None:
            return
        source = Path(template)
        if not source.is_file():
            raise PaletteError(
                ErrorCode.IMAGE_MISSING, "the profile's UEFI variable store is missing"
            )
        shutil.copyfile(source, paths.hardware_vars)

    # -- overlays --------------------------------------------------------------------
    async def create_overlay(
        self,
        paths: InstancePaths,
        *,
        base: Path | None = None,
        base_format: str | None = None,
    ) -> Path:
        """Create the writable layer for one instance and verify its backing chain.

        ``base`` overrides the profile's pinned image. The caller-supplied file becomes the
        read-only backing store, so it is validated for existence and shape but not against
        the profile's digest — the digest pins *the profile's* image, and a caller who names
        a different file has chosen to step outside that. The writable layer is still ours,
        still per-instance, and reset still throws it away.
        """
        overlay_format = "qcow2"
        backing_format: str
        if base is None:
            backing = (await asyncio.to_thread(self.require_base_image)).resolve()
            backing_format = self.profile.images.format
        else:
            backing = _validate_caller_image(base)
            backing_format = (base_format or self.profile.images.format).lower()
            if backing_format not in {"qcow2", "raw"}:
                raise PaletteError(
                    ErrorCode.IMAGE_INVALID,
                    f"unsupported backing format {backing_format!r} for a caller-supplied image",
                    details={"supported": ["qcow2", "raw"]},
                )
        if self.qemu_img is None:
            raise PaletteError(
                ErrorCode.QEMU_NOT_FOUND,
                "qemu-img was not found; it is required to create an instance overlay",
            )
        actual = await self.inspect_image(backing)
        if actual.format != backing_format:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID,
                "the base image's actual format does not match its declaration",
            )
        overlay = self.guard.resolve("vms", paths.vm_id, paths.instance_id, "disk", "overlay.qcow2")
        if overlay.exists():
            raise PaletteError(
                ErrorCode.STATE_CONFLICT, "refusing to overwrite an existing overlay"
            )
        argv = [
            *self.qemu_img_argv,
            "create",
            "-f",
            overlay_format,
            "-F",
            backing_format,
            "-b",
            str(backing),
            str(overlay),
        ]
        code, _stdout, stderr = await run_tool(argv, timeout=120.0)
        if code != 0:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID,
                "qemu-img could not create the instance overlay",
                details={"exit_code": code, "stderr_tail": stderr.strip()[-400:]},
            )
        info = await self._verify_backing_chain(overlay, backing, backing_format)
        self.update_manifest(
            paths,
            {
                "base_image": str(backing),
                "base_format": backing_format,
                "base_from_caller": base is not None,
                "overlay_format": overlay_format,
                "backing_chain": [str(path) for path in info.chain[1:]],
            },
        )
        return overlay

    async def inspect_image(self, path: Path, *, image_format: str | None = None) -> ImageInfo:
        """Inspect the actual format and full dependency chain before opening a disk."""
        if not self.qemu_img_argv:
            raise PaletteError(ErrorCode.QEMU_NOT_FOUND, "qemu-img is not configured")
        argv = [*self.qemu_img_argv, "info", "--backing-chain", "--output=json"]
        if image_format is not None:
            argv += ["-f", image_format]
        code, stdout, _stderr = await run_tool([*argv, str(path)], timeout=60.0)
        if code != 0:
            raise PaletteError(ErrorCode.IMAGE_INVALID, "qemu-img could not inspect the disk")
        try:
            parsed: Any = json.loads(stdout)
        except json.JSONDecodeError:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "qemu-img returned unreadable JSON"
            ) from None
        entries = parsed if isinstance(parsed, list) else [parsed]
        if not entries or any(not isinstance(entry, dict) for entry in entries):
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "qemu-img returned an invalid backing chain"
            )
        image_format = entries[0].get("format")
        if image_format not in {"raw", "qcow2"}:
            raise PaletteError(ErrorCode.IMAGE_INVALID, "only raw and qcow2 disks are supported")
        chain: list[Path] = []
        previous = path.resolve()
        for entry in entries:
            filename = entry.get("filename")
            if not isinstance(filename, str):
                raise PaletteError(ErrorCode.IMAGE_INVALID, "the disk chain has no filename")
            current = Path(filename)
            if not current.is_absolute():
                current = previous.parent / current
            previous = current.resolve()
            chain.append(previous)
        return ImageInfo(format=str(image_format), chain=tuple(chain))

    async def _verify_backing_chain(
        self, overlay: Path, base: Path, backing_format: str
    ) -> ImageInfo:
        """Fail unless the overlay's recorded backing file is exactly the trusted base."""
        if not self.qemu_img_argv:  # pragma: no cover - create_overlay checks first
            raise PaletteError(ErrorCode.QEMU_NOT_FOUND, "qemu-img is not configured")
        try:
            code, stdout, _stderr = await run_tool(
                [*self.qemu_img_argv, "info", "--output=json", str(overlay)], timeout=60.0
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "could not read back the overlay's backing chain"
            ) from exc
        if code != 0:
            raise PaletteError(ErrorCode.IMAGE_INVALID, "qemu-img info failed for the new overlay")
        try:
            parsed: Any = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "qemu-img info returned unreadable JSON"
            ) from None
        if not isinstance(parsed, dict):
            raise PaletteError(ErrorCode.IMAGE_INVALID, "qemu-img info returned unreadable JSON")
        info: dict[str, Any] = parsed
        backing = info.get("full-backing-filename", info.get("backing-filename"))
        actual = Path(backing) if isinstance(backing, str) else None
        if actual is not None and not actual.is_absolute():
            actual = overlay.parent / actual
        if actual is None or actual.resolve() != base.resolve():
            raise PaletteError(
                ErrorCode.IMAGE_INVALID,
                "the new overlay does not point at the trusted base image",
            )
        if info.get("format") != "qcow2" or info.get("backing-filename-format") != backing_format:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID,
                "the overlay's declared format or backing format is incorrect",
            )
        return await self.inspect_image(overlay, image_format="qcow2")

    # -- manifests -------------------------------------------------------------------
    def write_manifest(self, paths: InstancePaths, payload: dict[str, Any]) -> None:
        import os

        document = {
            "created_at": _iso_now(),
            **payload,
            "vm_id": paths.vm_id,
            "instance_id": paths.instance_id,
            "profile_id": self.profile.profile_id,
        }
        target = self.guard.resolve("vms", paths.vm_id, paths.instance_id, "manifest.json")
        temporary = self.guard.resolve("vms", paths.vm_id, paths.instance_id, "manifest.json.tmp")
        temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temporary, target)

    def update_manifest(self, paths: InstancePaths, changes: dict[str, Any]) -> None:
        document = self.read_manifest(paths.vm_id, paths.instance_id)
        if document is None:
            raise PaletteError(ErrorCode.PATH_DENIED, "the instance manifest is missing or invalid")
        self.write_manifest(paths, {**document, **changes, "updated_at": _iso_now()})

    def read_manifest(self, vm_id: str, instance_id: str) -> dict[str, Any] | None:
        try:
            path = self.guard.resolve("vms", vm_id, instance_id, "manifest.json")
        except PaletteError:
            return None
        if not path.is_file():
            return None
        try:
            parsed: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("unreadable manifest for %s/%s", vm_id, instance_id)
            return None
        if not isinstance(parsed, dict):
            return None
        if parsed.get("vm_id") != vm_id or parsed.get("instance_id") != instance_id:
            return None
        return parsed

    def list_instances(self, vm_id: str) -> list[dict[str, Any]]:
        validate_identifier(vm_id, what="vm_id")
        try:
            root = self.guard.resolve("vms", vm_id)
        except PaletteError:
            return []
        if not root.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            record: dict[str, Any] = {"instance_id": child.name, "root": str(child)}
            manifest = self.read_manifest(vm_id, child.name)
            if manifest is None:
                record["manifest_error"] = True
            else:
                record.update(manifest)
            try:
                record["overlay_bytes"] = _directory_bytes(
                    self.guard.resolve("vms", vm_id, child.name, "disk")
                )
            except PaletteError:
                record["manifest_error"] = True
            entries.append(record)
        return entries

    # -- cleanup ---------------------------------------------------------------------
    def delete_instance(
        self, vm_id: str, instance_id: str, *, require_manifest: bool = True
    ) -> bool:
        """Remove one instance directory.

        The target must be inside the runtime root *and* registered in a manifest, so a
        crafted identifier cannot reach a directory the service never created.
        """
        validate_identifier(vm_id, what="vm_id")
        validate_identifier(instance_id, what="instance_id")
        if require_manifest and self.read_manifest(vm_id, instance_id) is None:
            raise PaletteError(
                ErrorCode.PATH_DENIED,
                "refusing to delete an instance that has no manifest",
                details={"vm_id": vm_id},
            )
        root = self.guard.resolve("vms", vm_id, instance_id)
        self.guard.assert_managed(root)
        if not root.exists():
            return False
        shutil.rmtree(root, ignore_errors=False)
        return True

    def prune_instances(
        self, vm_id: str, *, keep_instance_ids: set[str], protected_paths: tuple[Path, ...] = ()
    ) -> list[str]:
        """Remove old instances beyond the retention limit, newest kept first."""
        records: dict[Path, dict[str, Any]] = {}
        vms = self.guard.resolve("vms")
        if not vms.is_dir():
            return []
        for directory in vms.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                for entry in self.list_instances(directory.name):
                    records[Path(entry["root"])] = entry
            except PaletteError:
                continue
        candidates = [
            entry
            for entry in records.values()
            if entry.get("vm_id") == vm_id
            and entry.get("instance_id") not in keep_instance_ids
            and entry.get("state") in {"STOPPED", "FAILED"}
            and entry.get("process_exited") is True
            and not entry.get("manifest_error")
        ]
        candidates.sort(key=lambda entry: str(entry.get("created_at", "")), reverse=True)
        surplus = candidates[self.max_overlays if self.keep_overlays else 0 :]
        pending = {Path(entry["root"]) for entry in surplus}

        def owners(paths: list[str] | tuple[Path, ...]) -> set[Path]:
            result: set[Path] = set()
            for path in paths:
                resolved = Path(path).resolve()
                result.update(root for root in records if resolved.is_relative_to(root))
            return result

        dependencies: dict[Path, set[Path]] = {}
        for root, entry in records.items():
            paths = [
                entry[key]
                for key in ("base_image", "firmware_code", "firmware_vars")
                if isinstance(entry.get(key), str)
            ]
            for key in ("backing_chain", "attached_volumes", "volume_backing_chain"):
                value = entry.get(key, [])
                if isinstance(value, list):
                    paths.extend(item for item in value if isinstance(item, str))
            dependencies[root] = owners(paths) - {root}

        def protect(roots: set[Path]) -> None:
            seen: set[Path] = set()
            while roots:
                root = roots.pop()
                if root in seen:
                    continue
                seen.add(root)
                pending.discard(root)
                roots.update(dependencies.get(root, set()) - seen)

        protect((set(records) - pending) | owners(protected_paths))
        removed: list[str] = []
        while pending:
            referenced = set().union(*(dependencies[root] for root in pending))
            ready = pending - referenced
            if not ready:
                logger.warning("retention kept a cyclic disk dependency for %s", vm_id)
                break
            root = min(ready, key=lambda path: str(records[path].get("created_at", "")))
            pending.remove(root)
            instance_id = str(records[root]["instance_id"])
            try:
                if self.delete_instance(vm_id, instance_id):
                    removed.append(instance_id)
            except (PaletteError, OSError) as exc:
                protect(set(dependencies[root]))
                logger.warning("prune skipped %s/%s: %s", vm_id, instance_id, type(exc).__name__)
        return removed


def _directory_bytes(path: Path) -> int:
    if not path.is_dir() or path.is_symlink():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat()
