"""In-process doubles for unit tests.

These are used where a real child process would make the test slow or unable to control
the exact failure being examined (a QMP error class, a capture that hangs, a frame that is
already stale). The end-to-end paths are covered separately by the fake QEMU process.

They are deliberately narrow: each one implements exactly the surface the production code
calls, so a test cannot accidentally depend on behaviour that does not exist.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from PIL import Image

from palette.capture.base import CapturedImage
from palette.errors import PaletteError, QmpCommandError
from palette.models import Size


def render_png(width: int, height: int, *, colour: tuple[int, int, int] = (200, 180, 90)) -> bytes:
    image = Image.new("RGB", (width, height), colour)
    # A border and a few marks so that two different renders are never byte-identical by
    # accident; the vm_wait heuristics depend on that difference being real.
    for x in range(width):
        image.putpixel((x, 0), (0, 0, 0))
        image.putpixel((x, height - 1), (0, 0, 0))
    for y in range(height):
        image.putpixel((0, y), (0, 0, 0))
        image.putpixel((width - 1, y), (0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_ppm(width: int, height: int, *, colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return header + bytes(colour) * (width * height)


class StubCaptureBackend:
    """Produces synthetic frames and can be scripted to fail on demand."""

    def __init__(
        self,
        *,
        width: int = 320,
        height: int = 200,
        colour: tuple[int, int, int] = (200, 180, 90),
        content_type: str = "png",
    ) -> None:
        self.width = width
        self.height = height
        self.colour = colour
        self.content_type = content_type
        self.calls = 0
        self.fail_with: PaletteError | None = None
        self.fail_after: int | None = None

    async def capture(self, *, display_id: str, head: int, sink: Path) -> CapturedImage:
        self.calls += 1
        if self.fail_with is not None and (self.fail_after is None or self.calls > self.fail_after):
            raise self.fail_with
        # The delivered payload is always PNG; ``content_type`` only records which
        # guest-side format produced the frame.
        payload = render_png(self.width, self.height, colour=self.colour)
        return CapturedImage(
            png_bytes=payload,
            native_size=Size(width=self.width, height=self.height),
            image_size=Size(width=self.width, height=self.height),
            format_used=self.content_type,
            scaled=False,
            duration_ms=1,
        )


class StubQmp:
    """A duck-typed stand-in for :class:`palette.qmp.client.QmpClient`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.results: dict[str, Any] = {}
        self.failures: dict[str, PaletteError] = {}
        self.fail_after: dict[str, int] = {}
        self.counts: dict[str, int] = {}
        self.connected = True

    async def execute(
        self, command: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None
    ):
        del timeout
        self.counts[command] = self.counts.get(command, 0) + 1
        self.calls.append((command, json.loads(json.dumps(arguments)) if arguments else arguments))
        limit = self.fail_after.get(command)
        if command in self.failures and (limit is None or self.counts[command] > limit):
            raise self.failures[command]
        return self.results.get(command, {})

    # -- inspection helpers ----------------------------------------------------------
    def events(self) -> list[list[dict[str, Any]]]:
        """Every ``input-send-event`` batch, flattened in order."""
        return [
            arguments["events"]
            for command, arguments in self.calls
            if command == "input-send-event" and arguments is not None
        ]

    def event_payloads(self) -> list[dict[str, Any]]:
        return [event for batch in self.events() for event in batch]

    def qcodes(self) -> list[tuple[str, bool]]:
        result = []
        for event in self.event_payloads():
            if event["type"] == "key":
                result.append((event["data"]["key"]["data"], event["data"]["down"]))
        return result

    def buttons(self) -> list[tuple[str, bool]]:
        result = []
        for event in self.event_payloads():
            if event["type"] == "btn":
                result.append((event["data"]["button"], event["data"]["down"]))
        return result

    def absolute(self) -> list[tuple[int, int]]:
        pending: dict[str, int] = {}
        positions: list[tuple[int, int]] = []
        for event in self.event_payloads():
            if event["type"] != "abs":
                continue
            axis = event["data"]["axis"]
            pending[axis] = event["data"]["value"]
            if "x" in pending and "y" in pending:
                positions.append((pending["x"], pending["y"]))
                pending = {}
        return positions


def format_rejection(
    desc: str = "screendump: Parameter 'format' expects 'ImageFormat'",
) -> QmpCommandError:
    return QmpCommandError("GenericError", desc, "screendump")
