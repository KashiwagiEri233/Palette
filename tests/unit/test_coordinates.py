"""Coordinate mapping: endpoints, rounding, rejection and refusal to approximate."""

from __future__ import annotations

import pytest

from palette.errors import ErrorCode, PaletteError
from palette.input.coordinates import CoordinateTransform, interpolate, transform_id_for
from palette.models import QEMU_ABS_MAX, Size, Viewport


def transform(image: tuple[int, int], native: tuple[int, int]) -> CoordinateTransform:
    return CoordinateTransform.full(
        Size(width=image[0], height=image[1]), Size(width=native[0], height=native[1])
    )


class TestEndpoints:
    def test_origin_maps_to_origin(self) -> None:
        assert transform((320, 200), (320, 200)).to_qemu(0, 0) == (0, 0)

    def test_last_pixel_maps_to_last_axis_value(self) -> None:
        # The plan's use of size-1 is what makes the final pixel reachable; a naive
        # x * 32767 / width would leave the bottom-right corner permanently unreachable.
        assert transform((320, 200), (320, 200)).to_qemu(319, 199) == (QEMU_ABS_MAX, QEMU_ABS_MAX)

    def test_scaled_image_endpoints_agree(self) -> None:
        scaled = transform((160, 100), (320, 200))
        assert scaled.to_qemu(0, 0) == (0, 0)
        assert scaled.to_qemu(159, 99) == (QEMU_ABS_MAX, QEMU_ABS_MAX)

    def test_rounding_matches_the_documented_formula(self) -> None:
        # native 320 wide: qx = round(x * 32767 / 319)
        assert transform((320, 200), (320, 200)).to_qemu(1, 0)[0] == round(1 * 32767 / 319)
        assert transform((320, 200), (320, 200)).to_qemu(160, 0)[0] == round(160 * 32767 / 319)

    def test_mapping_is_monotonic(self) -> None:
        plane = transform((320, 200), (1280, 800))
        values = [plane.to_qemu(x, 0)[0] for x in range(320)]
        assert values == sorted(values)
        assert len(set(values)) > 300  # distinct pixels stay distinct

    def test_every_result_stays_inside_the_axis_range(self) -> None:
        plane = transform((333, 211), (1280, 800))
        for x in range(0, 333, 7):
            for y in range(0, 211, 5):
                qx, qy = plane.to_qemu(x, y)
                assert 0 <= qx <= QEMU_ABS_MAX
                assert 0 <= qy <= QEMU_ABS_MAX


class TestRejection:
    def test_coordinate_beyond_the_right_edge_is_refused(self) -> None:
        with pytest.raises(PaletteError) as info:
            transform((320, 200), (320, 200)).to_qemu(320, 10)
        assert info.value.code is ErrorCode.INVALID_COORDINATES
        # Explicitly not clamped: an agent must learn its coordinate was wrong rather than
        # have its click silently relocated to the screen edge.
        assert info.value.details["max_x"] == 319

    def test_negative_coordinate_is_refused(self) -> None:
        with pytest.raises(PaletteError) as info:
            transform((320, 200), (320, 200)).to_qemu(-1, 0)
        assert info.value.code is ErrorCode.INVALID_COORDINATES

    @pytest.mark.parametrize("value", [1.5, "10", None, True])
    def test_non_integer_coordinates_are_refused(self, value: object) -> None:
        with pytest.raises(PaletteError) as info:
            transform((320, 200), (320, 200)).to_qemu(value, 0)  # type: ignore[arg-type]
        assert info.value.code is ErrorCode.INVALID_COORDINATES

    def test_a_one_pixel_image_cannot_define_a_space(self) -> None:
        with pytest.raises(PaletteError) as info:
            CoordinateTransform.full(Size(width=1, height=200), Size(width=320, height=200))
        assert info.value.code is ErrorCode.INVALID_COORDINATES


class TestViewport:
    def test_a_cropped_viewport_is_refused_rather_than_approximated(self) -> None:
        with pytest.raises(PaletteError) as info:
            CoordinateTransform(
                image_size=Size(width=100, height=100),
                native_size=Size(width=320, height=200),
                viewport=Viewport(x=10, y=10, width=100, height=100),
                transform_id="crop",
            )
        assert info.value.code is ErrorCode.NOT_IMPLEMENTED

    def test_full_viewport_is_accepted(self) -> None:
        plane = transform((320, 200), (320, 200))
        assert plane.viewport.model_dump() == {"x": 0, "y": 0, "width": 320, "height": 200}


class TestTransformId:
    def test_unscaled_frames_get_a_full_id(self) -> None:
        assert (
            transform_id_for(Size(width=1280, height=800), Size(width=1280, height=800))
            == "full_1280x800"
        )

    def test_scaled_frames_record_both_sizes(self) -> None:
        identity = transform_id_for(Size(width=640, height=400), Size(width=1280, height=800))
        assert identity == "fit_640x400_of_1280x800"


class TestInterpolation:
    def test_a_two_point_path_is_densified(self) -> None:
        path = interpolate([(0, 0), (10, 0)], steps_per_segment=2)
        assert path == [(0, 0), (5, 0), (10, 0)]

    def test_intermediate_points_are_preserved(self) -> None:
        path = interpolate([(0, 0), (0, 10), (10, 10)], steps_per_segment=1)
        assert path == [(0, 0), (0, 10), (10, 10)]

    def test_a_single_point_is_returned_unchanged(self) -> None:
        assert interpolate([(3, 4)], steps_per_segment=4) == [(3, 4)]
