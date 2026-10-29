"""Expose the original persistent disk through the ordinary drive catalog."""

from __future__ import annotations

from .definitions import VmDefinition
from .vm_settings import DriveSettings, HardwareSettings

PRIMARY_DRIVE_ID = "drive-primary"


def primary_present(definition: VmDefinition) -> bool:
    return not definition.drives_initialized or bool(
        definition.draft.settings
        and any(drive.id == PRIMARY_DRIVE_ID for drive in definition.draft.settings.drives)
    )


def include_primary(definition: VmDefinition, settings: HardwareSettings) -> HardwareSettings:
    if definition.drives_initialized:
        return settings
    drive = DriveSettings(
        id=PRIMARY_DRIVE_ID,
        media="disk",
        size_gib=definition.draft.disk_gib,
        format="qcow2",
        read_only=False,
        interface="virtio",
    )
    return settings.model_copy(update={"drives": [drive, *settings.drives]})
