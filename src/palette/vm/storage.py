"""Shared disk references and synchronous retention transactions.

Reference changes and pruning do not yield to the event loop. They are therefore atomic
with respect to every VM operation, while a reference can protect a long async startup.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import PaletteConfig, Profile
from ..errors import PaletteError
from ..policy import PathGuard
from .images import ImageManager, InstancePaths
from .launcher import LaunchOverrides

if TYPE_CHECKING:
    from .manager import VmInstance
logger = logging.getLogger("palette.vm.storage")


class VmStorage:
    def __init__(self, config: PaletteConfig) -> None:
        self.guard = PathGuard(config.runtime_root)
        self.persistent_roots: set[Path] = set()
        self._references: dict[Path, int] = {}

    @contextmanager
    def protect(
        self, profile: Profile, overrides: LaunchOverrides, storage: InstancePaths | None
    ) -> Iterator[None]:
        paths = {Path(profile.images.base).resolve()}
        if overrides.image is not None:
            paths.add(overrides.image.resolve())
        paths.update(Path(volume.path).resolve() for volume in overrides.volumes if volume.path)
        if storage is not None:
            paths.add(storage.overlay.resolve())
            if storage.hardware_vars is not None:
                paths.add(storage.hardware_vars.resolve())
        for path in paths:
            self._references[path] = self._references.get(path, 0) + 1
        try:
            yield
        finally:
            for path in paths:
                remaining = self._references[path] - 1
                if remaining:
                    self._references[path] = remaining
                else:
                    self._references.pop(path)

    def images_for(self, profile: Profile) -> ImageManager:
        return ImageManager(
            self.guard,
            profile,
            max_overlays=profile.retention.max_overlays,
            keep_overlays=profile.retention.keep_overlays,
        )

    def prune(
        self,
        vm_id: str,
        profile: Profile,
        instances: Sequence[VmInstance],
        *,
        delete_all: bool = False,
    ) -> None:
        keep = {
            instance.view.instance_id
            for instance in instances
            if instance.view.vm_id == vm_id
            and (
                instance.state not in {"STOPPED", "FAILED"}
                or (instance.process is not None and instance.process.returncode is None)
            )
        }
        protected: list[Path] = list(self.persistent_roots) + list(self._references)
        for instance in instances:
            if instance.process is not None and instance.process.returncode is None:
                protected.append(instance.paths.root)
                protected.append(instance.paths.overlay)
                protected.append(instance.overrides.image or Path(instance.profile.images.base))
                protected.extend(
                    Path(volume.path) for volume in instance.overrides.volumes if volume.path
                )
        images = self.images_for(profile)
        if delete_all:
            images.keep_overlays = False
            images.max_overlays = 0
        try:
            images.prune_instances(vm_id, keep_instance_ids=keep, protected_paths=tuple(protected))
        except (PaletteError, OSError) as exc:
            if delete_all:
                raise
            logger.warning("retention deferred for %s: %s", vm_id, type(exc).__name__)
