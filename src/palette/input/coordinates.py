"""Image-space to QEMU absolute-axis coordinate mapping.

There is exactly one coordinate space in the tool API: **the pixels of the image the agent
received**, origin top-left, X right, Y down. Host screen coordinates, guest logical DPI
and raw framebuffer pixels are all different spaces and are never mixed with it.

Mapping follows the plan's formula, including its deliberate use of ``size - 1``: the
transform maps the *last* image pixel onto the *last* framebuffer pixel, so a click at the
far edge of a scaled image does not land one pixel short of the edge in the guest::

    fx = x * (Fw - 1) / (Iw - 1)
    fy = y * (Fh - 1) / (Ih - 1)
    qx = round(fx * 32767 / (Fw - 1))
    qy = round(fy * 32767 / (Fh - 1))

Out-of-range coordinates are **rejected**, never clamped: silently moving an intended
click to the screen edge is worse than an error the agent must handle.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ErrorCode, PaletteError
from ..models import QEMU_ABS_MAX, Frame, Size, Viewport


@dataclass(frozen=True, slots=True)
class CoordinateTransform:
    """The mapping attached to one frame. Actions must use the transform of *their own*
    frame, not whatever the current default size happens to be."""

    image_size: Size
    native_size: Size
    viewport: Viewport
    transform_id: str

    @classmethod
    def from_frame(cls, frame: Frame) -> CoordinateTransform:
        return cls(
            image_size=frame.image_size,
            native_size=frame.native_size,
            viewport=frame.viewport,
            transform_id=frame.transform_id,
        )

    @classmethod
    def full(cls, image_size: Size, native_size: Size) -> CoordinateTransform:
        return cls(
            image_size=image_size,
            native_size=native_size,
            viewport=Viewport.full(native_size),
            transform_id=transform_id_for(image_size, native_size),
        )

    def __post_init__(self) -> None:
        if self.image_size.width < 2 or self.image_size.height < 2:
            raise PaletteError(
                ErrorCode.INVALID_COORDINATES,
                "an image must be at least 2x2 pixels to define a coordinate space",
            )
        if self.native_size.width < 2 or self.native_size.height < 2:
            raise PaletteError(
                ErrorCode.INVALID_COORDINATES,
                "the framebuffer must be at least 2x2 pixels to define a coordinate space",
            )
        # The MVP delivers the whole surface. A cropped viewport would need the mapping
        # to be composed with the crop origin first; reusing the uncropped formula would
        # silently misplace every click, so it is refused rather than approximated.
        if (
            self.viewport.x != 0
            or self.viewport.y != 0
            or self.viewport.width != self.native_size.width
            or self.viewport.height != self.native_size.height
        ):
            raise PaletteError(
                ErrorCode.NOT_IMPLEMENTED,
                "coordinate mapping for a cropped viewport is not implemented",
                details={
                    "viewport": self.viewport.model_dump(),
                    "native_size": self.native_size.as_dict(),
                },
            )

    # -- validation ------------------------------------------------------------------
    def bounds(self) -> tuple[int, int]:
        return self.image_size.width - 1, self.image_size.height - 1

    def validate(self, x: int, y: int) -> None:
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, int)
            or not isinstance(y, int)
        ):
            raise PaletteError(ErrorCode.INVALID_COORDINATES, "coordinates must be integers")
        max_x, max_y = self.bounds()
        if not (0 <= x <= max_x and 0 <= y <= max_y):
            raise PaletteError(
                ErrorCode.INVALID_COORDINATES,
                "the coordinate is outside the image the action was derived from",
                details={"x": x, "y": y, "max_x": max_x, "max_y": max_y},
            )

    # -- mapping ---------------------------------------------------------------------
    def to_qemu(self, x: int, y: int) -> tuple[int, int]:
        """Map an image pixel to the 0..32767 absolute axis pair."""
        self.validate(x, y)
        image_w, image_h = self.image_size.width, self.image_size.height
        native_w, native_h = self.native_size.width, self.native_size.height

        fx = x * (native_w - 1) / (image_w - 1)
        fy = y * (native_h - 1) / (image_h - 1)
        qx = round(fx * QEMU_ABS_MAX / (native_w - 1))
        qy = round(fy * QEMU_ABS_MAX / (native_h - 1))
        # Floating point cannot push these outside the axis range, but the invariant is
        # cheap to assert and would catch a future change to the formula.
        return (
            min(QEMU_ABS_MAX, max(0, qx)),
            min(QEMU_ABS_MAX, max(0, qy)),
        )

    def describe(self) -> dict[str, object]:
        return {
            "transform_id": self.transform_id,
            "image_size": self.image_size.as_dict(),
            "native_size": self.native_size.as_dict(),
            "viewport": self.viewport.model_dump(),
        }


def transform_id_for(image_size: Size, native_size: Size) -> str:
    if image_size == native_size:
        return f"full_{native_size.width}x{native_size.height}"
    return f"fit_{image_size.width}x{image_size.height}_of_{native_size.width}x{native_size.height}"


def interpolate(points: list[tuple[int, int]], *, steps_per_segment: int) -> list[tuple[int, int]]:
    """Densify a drag path so the guest sees motion rather than a teleport.

    A single absolute jump followed by a release often registers as a click at the
    destination; intermediate samples give the guest's input stack something to track.
    """
    if len(points) < 2:
        return list(points)
    result: list[tuple[int, int]] = [points[0]]
    for index in range(len(points) - 1):
        start_x, start_y = points[index]
        end_x, end_y = points[index + 1]
        for step in range(1, steps_per_segment + 1):
            ratio = step / steps_per_segment
            result.append(
                (
                    round(start_x + (end_x - start_x) * ratio),
                    round(start_y + (end_y - start_y) * ratio),
                )
            )
    return result
