"""QMP capture: the PNG/PPM fallback, size limits and the errors it must not confuse."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from palette.capture.qmp import QmpCaptureBackend
from palette.config import CaptureConfig
from palette.errors import ErrorCode, PaletteError, QmpCommandError
from palette.models import Size
from support.doubles import render_png, render_ppm


class ScreendumpQmp:
    """Records screendump calls and writes the payload the test chose."""

    def __init__(
        self,
        *,
        payload: bytes | None = None,
        png_supported: bool = True,
        error: PaletteError | None = None,
        produce_file: bool = True,
    ) -> None:
        self.payload = payload
        self.png_supported = png_supported
        self.error = error
        self.produce_file = produce_file
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self, command: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None
    ):
        del timeout
        if command != "screendump":
            return {}
        self.calls.append(dict(arguments or {}))
        arguments = arguments or {}
        if arguments.get("format") == "png" and not self.png_supported:
            raise QmpCommandError(
                "GenericError",
                "screendump: Parameter 'format' expects 'ImageFormat' but png is not supported",
                "screendump",
            )
        if self.error is not None:
            raise self.error
        if self.produce_file:
            target = Path(str(arguments["filename"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = self.payload
            if payload is None:
                payload = (
                    render_png(320, 200)
                    if arguments.get("format") == "png"
                    else render_ppm(320, 200)
                )
            target.write_bytes(payload)
        return {}


def backend(client: ScreendumpQmp, **overrides) -> QmpCaptureBackend:
    config = CaptureConfig(
        max_long_edge=overrides.pop("max_long_edge", 1280),
        max_bytes=overrides.pop("max_bytes", 4 * 1024 * 1024),
        timeout_seconds=5.0,
        keep_frames=overrides.pop("keep_frames", False),
        **overrides,
    )
    return QmpCaptureBackend(client, config)  # type: ignore[arg-type]


class TestHappyPath:
    async def test_png_capture_reports_the_framebuffer_size(self, tmp_path: Path) -> None:
        client = ScreendumpQmp(payload=render_png(1280, 800))
        result = await backend(client).capture(display_id="video0", head=0, sink=tmp_path / "f.png")
        assert result.format_used == "png"
        assert result.native_size == Size(width=1280, height=800)
        assert result.image_size == result.native_size
        assert result.scaled is False
        assert result.png_bytes.startswith(b"\x89PNG")

    async def test_the_command_names_the_display_device_not_the_keyboard(
        self, tmp_path: Path
    ) -> None:
        """input-send-event and screendump both address the display id; sending the
        keyboard's id is a silent routing mistake, so it is asserted directly."""
        client = ScreendumpQmp()
        await backend(client).capture(display_id="video0", head=0, sink=tmp_path / "f.png")
        assert client.calls[0]["device"] == "video0"
        assert client.calls[0]["head"] == 0
        assert client.calls[0]["format"] == "png"

    async def test_the_filename_is_the_one_the_service_generated(self, tmp_path: Path) -> None:
        client = ScreendumpQmp()
        sink = tmp_path / "frames" / "frame-1.png"
        await backend(client).capture(display_id="video0", head=0, sink=sink)
        assert client.calls[0]["filename"] == str(sink)


class TestFallback:
    async def test_a_png_format_rejection_falls_back_to_ppm(self, tmp_path: Path) -> None:
        client = ScreendumpQmp(png_supported=False, payload=render_ppm(320, 200))
        result = await backend(client).capture(display_id="video0", head=0, sink=tmp_path / "f.png")
        assert result.format_used == "ppm"
        assert [call.get("format") for call in client.calls] == ["png", None]
        # The delivered image is still PNG; the guest-side format is an implementation
        # detail the agent never sees.
        assert result.png_bytes.startswith(b"\x89PNG")

    async def test_the_failing_call_is_only_paid_once(self, tmp_path: Path) -> None:
        client = ScreendumpQmp(png_supported=False)
        capture = backend(client)
        await capture.capture(display_id="video0", head=0, sink=tmp_path / "a.png")
        client.calls.clear()
        await capture.capture(display_id="video0", head=0, sink=tmp_path / "b.png")
        assert [call.get("format") for call in client.calls] == [None]

    async def test_fallback_can_be_disabled(self, tmp_path: Path) -> None:
        client = ScreendumpQmp(png_supported=False)
        capture = backend(client, allow_ppm_fallback=False)
        with pytest.raises(PaletteError) as info:
            await capture.capture(display_id="video0", head=0, sink=tmp_path / "f.png")
        assert info.value.code is ErrorCode.UNSUPPORTED_CAPABILITY

    async def test_a_disk_error_is_not_mistaken_for_a_format_problem(self, tmp_path: Path) -> None:
        """Only a format rejection may trigger the PPM retry. A missing file or a dead VM
        must surface as itself, or a broken capture would look like a supported one."""
        client = ScreendumpQmp(
            error=QmpCommandError("GenericError", "could not open file", "screendump")
        )
        with pytest.raises(PaletteError) as info:
            await backend(client).capture(display_id="video0", head=0, sink=tmp_path / "f.png")
        assert info.value.code is ErrorCode.CAPTURE_FAILED
        assert len(client.calls) == 1
        assert info.value.details["qmp_class"] == "GenericError"

    async def test_a_device_error_is_not_mistaken_for_a_format_problem(
        self, tmp_path: Path
    ) -> None:
        client = ScreendumpQmp(error=QmpCommandError("DeviceNotFound", "Device 'video0' not found"))
        with pytest.raises(PaletteError) as info:
            await backend(client).capture(display_id="video0", head=0, sink=tmp_path / "f.png")
        assert info.value.code is ErrorCode.CAPTURE_FAILED
        assert len(client.calls) == 1


class TestFiles:
    async def test_a_successful_command_without_a_file_is_a_capture_failure(
        self, tmp_path: Path
    ) -> None:
        client = ScreendumpQmp(produce_file=False)
        with pytest.raises(PaletteError) as info:
            await backend(client).capture(display_id="video0", head=0, sink=tmp_path / "f.png")
        assert info.value.code is ErrorCode.CAPTURE_FAILED

    async def test_the_frame_file_is_removed_by_default(self, tmp_path: Path) -> None:
        sink = tmp_path / "f.png"
        await backend(ScreendumpQmp()).capture(display_id="video0", head=0, sink=sink)
        assert not sink.exists()

    async def test_retention_keeps_the_file_when_asked(self, tmp_path: Path) -> None:
        sink = tmp_path / "f.png"
        result = await backend(ScreendumpQmp(), keep_frames=True).capture(
            display_id="video0", head=0, sink=sink
        )
        assert sink.exists()
        assert result.source_path == sink

    async def test_an_empty_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(PaletteError) as info:
            await backend(ScreendumpQmp(payload=b"")).capture(
                display_id="video0", head=0, sink=tmp_path / "f.png"
            )
        assert info.value.code is ErrorCode.CAPTURE_FAILED

    async def test_a_non_image_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(PaletteError) as info:
            await backend(ScreendumpQmp(payload=b"not an image at all")).capture(
                display_id="video0", head=0, sink=tmp_path / "f.png"
            )
        assert info.value.code is ErrorCode.CAPTURE_FAILED


class TestLimits:
    async def test_a_large_framebuffer_is_scaled_and_the_mapping_follows(
        self, tmp_path: Path
    ) -> None:
        client = ScreendumpQmp(payload=render_png(1600, 1000))
        result = await backend(client, max_long_edge=800).capture(
            display_id="video0", head=0, sink=tmp_path / "f.png"
        )
        assert result.native_size == Size(width=1600, height=1000)
        assert result.image_size.width == 800
        assert result.image_size.height == 500
        assert result.scaled is True

    async def test_no_scaling_happens_when_the_image_already_fits(self, tmp_path: Path) -> None:
        result = await backend(
            ScreendumpQmp(payload=render_png(800, 600)), max_long_edge=1280
        ).capture(display_id="video0", head=0, sink=tmp_path / "f.png")
        assert result.scaled is False
        assert result.image_size == Size(width=800, height=600)

    async def test_an_impossible_byte_limit_is_reported_rather_than_exceeded(
        self, tmp_path: Path
    ) -> None:
        client = ScreendumpQmp(payload=render_png(1024, 768))
        with pytest.raises(PaletteError) as info:
            await backend(client, max_bytes=1024, max_long_edge=1024).capture(
                display_id="video0", head=0, sink=tmp_path / "f.png"
            )
        assert info.value.code is ErrorCode.CAPTURE_TOO_LARGE

    async def test_an_enormous_framebuffer_is_refused_before_decoding(self, tmp_path: Path) -> None:
        """A decompression bomb from an unexpected display surface is bounded."""
        client = ScreendumpQmp(payload=render_png(9000, 5000))
        with pytest.raises(PaletteError) as info:
            await backend(client, max_long_edge=1280).capture(
                display_id="video0", head=0, sink=tmp_path / "f.png"
            )
        assert info.value.code is ErrorCode.CAPTURE_TOO_LARGE
