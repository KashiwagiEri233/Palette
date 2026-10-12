"""Base images, per-instance overlays, manifests and retention.

The base image is the only durable artefact. Every run gets its own writable qcow2
overlay with an explicitly declared backing format and path, and the base is never
committed to: "reset" means *throw the overlay away and make a new one*, which is a much
stronger statement than an in-guest reboot.

Deletion only ever touches objects that (a) appear in a manifest and (b) resolve inside
the managed runtime root, so a mistaken identifier cannot turn into a recursive delete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
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
        expected_suffix = "." + self.profile.images.format
        if base.suffix.lower() != expected_suffix and not (
            self.profile.images.format == "qcow2" and base.suffix.lower() == ".qcow2"
        ):
            info["ok"] = False
            info["reason"] = f"extension does not match declared format {expected_suffix}"
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
        for directory in (root, disk_dir, frames, logs):
            directory.mkdir(parents=True, exist_ok=True)
        hardware_vars: Path | None = None
        if self.profile.qemu.firmware.vars:
            hardware_vars = disk_dir / "vars.fd"
        return InstancePaths(
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
        overlay_format = self.profile.images.format
        backing_format: str
        if base is None:
            backing = self.require_base_image()
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
        self._verify_backing_chain(overlay, backing)
        return overlay

    def _verify_backing_chain(self, overlay: Path, base: Path) -> None:
        """Fail unless the overlay's recorded backing file is exactly the trusted base."""
        if not self.qemu_img_argv:  # pragma: no cover - create_overlay checks first
            raise PaletteError(ErrorCode.QEMU_NOT_FOUND, "qemu-img is not configured")
        try:
            result = run_sync(
                [*self.qemu_img_argv, "info", "--output=json", str(overlay)], timeout=60.0
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "could not read back the overlay's backing chain"
            ) from exc
        if result.returncode != 0:
            raise PaletteError(ErrorCode.IMAGE_INVALID, "qemu-img info failed for the new overlay")
        try:
            parsed: Any = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "qemu-img info returned unreadable JSON"
            ) from None
        if not isinstance(parsed, dict):
            raise PaletteError(ErrorCode.IMAGE_INVALID, "qemu-img info returned unreadable JSON")
        info: dict[str, Any] = parsed
        backing = info.get("backing-filename")
        if backing is None or Path(backing).resolve() != base.resolve():
            raise PaletteError(
                ErrorCode.IMAGE_INVALID,
                "the new overlay does not point at the trusted base image",
            )
        backing_format = info.get("backing-filename-format")
        if backing_format and backing_format != self.profile.images.format:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID,
                "the overlay's declared backing format does not match the profile",
            )

    # -- manifests -------------------------------------------------------------------
    def write_manifest(self, paths: InstancePaths, payload: dict[str, Any]) -> None:
        import os

        document = {
            "vm_id": paths.vm_id,
            "instance_id": paths.instance_id,
            "profile_id": self.profile.profile_id,
            "created_at": _iso_now(),
            **payload,
        }
        target = paths.manifest
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temporary, target)

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
        return parsed if isinstance(parsed, dict) else None

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
            if not child.is_dir():
                continue
            manifest = child / "manifest.json"
            record: dict[str, Any] = {"instance_id": child.name, "root": str(child)}
            if manifest.is_file():
                try:
                    record.update(json.loads(manifest.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    record["manifest_error"] = True
            record["overlay_bytes"] = _directory_bytes(child / "disk")
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

    def prune_instances(self, vm_id: str, *, keep_instance_ids: set[str]) -> list[str]:
        """Remove old instances beyond the retention limit, newest kept first."""
        if self.keep_overlays is False:
            keep_instance_ids = set(keep_instance_ids)
        candidates = [
            entry
            for entry in self.list_instances(vm_id)
            if entry.get("instance_id") not in keep_instance_ids
        ]
        candidates.sort(key=lambda entry: str(entry.get("created_at", "")), reverse=True)
        surplus = candidates[self.max_overlays :]
        removed: list[str] = []
        for entry in surplus:
            instance_id = str(entry.get("instance_id"))
            try:
                if self.delete_instance(vm_id, instance_id):
                    removed.append(instance_id)
            except PaletteError as exc:
                logger.warning("prune skipped %s/%s: %s", vm_id, instance_id, exc.code)
        return removed


def _directory_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
