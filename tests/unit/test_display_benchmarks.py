import copy
import json

import pytest
from scripts.compare_display_reports import compare, main, resources


def reports(provider: str, latency: float) -> list[dict]:
    return [
        {
            "kind": "desktop-display",
            "provider": provider,
            "complete": True,
            "host": {"os": "test", "arch": "x64"},
            "execution": {"vcpus": 2, "memory_mib": 4096, "qemu_version": "fixture"},
            "guest": "fixture-v1",
            "guest_arch": "x86_64",
            "accelerator": "test",
            "width": width,
            "height": height,
            "rounds": [
                {
                    "round": index,
                    "load": load,
                    "input_to_visible_ms": [latency] * 100,
                    "input_to_ack_ms": [1.0] * 100,
                    "observation_ms": [10.0] * 100,
                    "shadow_observation_ms": [latency] * 100,
                    "drag_frame_intervals_ms": [16.0] * 20,
                    "scroll_frame_intervals_ms": [16.0] * 20,
                    "resources": [
                        {
                            "at": at,
                            "incomplete": False,
                            "processes": [
                                {
                                    "pid": 1,
                                    "created_at": 1,
                                    "cpu_seconds": at / 10000,
                                    "rss_bytes": 100,
                                }
                            ],
                        }
                        for at in (1000, 2000)
                    ],
                }
                for load in ("idle", "observe")
                for index in (1, 2, 3)
            ],
        }
        for width, height in ((1280, 800), (1920, 1080))
    ]


def test_gate_requires_full_matrix_and_never_promotes_shadow_frames() -> None:
    result = compare(reports("rfb", 10), reports("raw", 7))
    assert result["performance_gate_passed"] is True
    assert result["migration_recommended"] is False
    with pytest.raises(ValueError, match="both"):
        compare(reports("rfb", 10)[:1], reports("raw", 7))


def test_regressions_and_missing_measurements_cannot_pass() -> None:
    assert not compare(reports("rfb", 10), reports("raw", 12))["performance_gate_passed"]
    incomplete = copy.deepcopy(reports("raw", 7))
    incomplete[0]["rounds"][0]["input_to_visible_ms"] = []
    with pytest.raises(ValueError, match="insufficient"):
        compare(reports("rfb", 10), incomplete)
    incomplete = reports("raw", 7)
    incomplete[0]["rounds"][0]["resources"][1]["processes"][0]["pid"] = 2
    with pytest.raises(ValueError, match="membership"):
        compare(reports("rfb", 10), incomplete)
    baseline = reports("rfb", 10)
    candidate = reports("raw", 7)
    for report in baseline + candidate:
        del report["guest"]
    with pytest.raises(ValueError, match="missing"):
        compare(baseline, candidate)


def detailed_reports(
    variant: str, motion_latency: float, observation_pipeline: str = "split"
) -> list[dict]:
    result = reports("raw" if variant == "raw" else "rfb", 10)
    for report in result:
        report["schema_version"] = 2
        report["variant"] = variant
        report["observation_pipeline"] = observation_pipeline
        report["vm_id"] = "fixture-vm"
        report["instance_id"] = "fixture-instance"
        report["display_settings"] = {
            "instances": [
                {
                    "vm_id": "fixture-vm",
                    "instance_id": "fixture-instance",
                    "provider": report["provider"],
                    "refresh_mode": "adaptive" if variant == "adaptive" else "steady",
                }
            ]
        }
        report["protocol"] = {
            "samples": 100,
            "rounds": 3,
            "motion_hz": 60,
            "idle_seconds": [10, 120],
            "wake_samples": 1,
        }
        for row in report["rounds"]:
            row["numbered_inputs"] = [{"id": index} for index in range(100)]
            for phase in ("drag", "scroll"):
                matched = [
                    {
                        "id": index,
                        "input_at": float(index),
                        "visible_at": index + motion_latency,
                        "latency_ms": motion_latency,
                    }
                    for index in range(99, -1, -5)
                ][::-1]
                row[phase] = {
                    "issued": [{"id": index, "at": float(index)} for index in range(100)],
                    "samples": matched,
                    "input_to_visible_ms": [motion_latency] * len(matched),
                    "settle_ms": motion_latency,
                    "merged_or_not_presented": 100 - len(matched),
                }
            row["idle_wake"] = []
            for seconds in (10, 120):
                idle_resources = copy.deepcopy(row["resources"])
                idle_resources[-1]["at"] = idle_resources[0]["at"] + seconds * 1000
                row["idle_wake"].append(
                    {
                        "idle_seconds": seconds,
                        "input_kind": ("key", "drag", "scroll")[(row["round"] - 1) % 3],
                        "latency_ms": 10,
                        "resources": idle_resources,
                    }
                )
    return result


def test_steady_vnc_requires_improvement_in_both_matched_motion_paths() -> None:
    baseline, candidate = detailed_reports("adaptive", 100), detailed_reports("steady", 70)
    result = compare(baseline, candidate)
    assert result["performance_gate_passed"]
    assert result["candidate_variant"] == "steady"
    assert result["comparison_scope"] == "display_refresh_only"
    assert not result["combined_optimization_gate_passed"]
    assert "local steady-refresh" in result["conclusion"]
    for report in candidate:
        for row in report["rounds"]:
            row["scroll"] = copy.deepcopy(baseline[0]["rounds"][0]["scroll"])
    assert not compare(baseline, candidate)["performance_gate_passed"]


def test_motion_and_wake_regressions_cannot_hide_behind_space_improvement() -> None:
    baseline, candidate = detailed_reports("adaptive", 100), detailed_reports("steady", 70)
    candidate[0]["rounds"][0]["drag_frame_intervals_ms"] = [30] * 20
    candidate[0]["rounds"][1]["drag_frame_intervals_ms"] = [30] * 20
    result = compare(baseline, candidate)
    assert not result["performance_gate_passed"]
    assert any(row["metric"] == "drag_frame" for row in result["regressions"])
    candidate = detailed_reports("steady", 70)
    for report in candidate:
        for row in report["rounds"]:
            row["idle_wake"][0]["latency_ms"] = 20
    assert not compare(baseline, candidate)["performance_gate_passed"]


def test_incomplete_protocol_and_unmatched_final_motion_are_rejected() -> None:
    baseline, candidate = detailed_reports("adaptive", 100), detailed_reports("steady", 70)
    candidate[0]["protocol"]["idle_seconds"] = [10]
    with pytest.raises(ValueError, match="protocol"):
        compare(baseline, candidate)
    candidate = detailed_reports("steady", 70)
    candidate[0]["rounds"][0]["drag"]["samples"][-1]["id"] = 98
    with pytest.raises(ValueError, match=r"matched|final"):
        compare(baseline, candidate)
    candidate = detailed_reports("steady", 70)
    candidate[0]["rounds"][0]["idle_wake"][1]["resources"][-1]["at"] = 2000
    with pytest.raises(ValueError, match="shorter"):
        compare(baseline, candidate)


def test_legacy_raw_frame_interval_regression_is_now_checked() -> None:
    baseline, candidate = reports("rfb", 10), reports("raw", 7)
    for report in candidate:
        for row in report["rounds"]:
            row["scroll_frame_intervals_ms"] = [50] * 20
    assert not compare(baseline, candidate)["performance_gate_passed"]


def test_combined_pipeline_and_actual_instance_are_part_of_the_evidence() -> None:
    baseline = detailed_reports("adaptive", 100, observation_pipeline="serial")
    candidate = detailed_reports("steady", 70)
    result = compare(baseline, candidate)
    assert result["combined_optimization_gate_passed"]
    assert result["comparison_scope"] == "display_and_observation"
    candidate[0]["display_settings"]["instances"][0]["refresh_mode"] = "adaptive"
    with pytest.raises(ValueError, match="actual display"):
        compare(baseline, candidate)
    candidate = detailed_reports("steady", 70)
    candidate[0]["observation_pipeline"] = "serial"
    with pytest.raises(ValueError, match="one observation pipeline"):
        compare(baseline, candidate)


def test_resource_cpu_uses_one_core_percent_and_rejects_invalid_middle_samples() -> None:
    samples = reports("rfb", 10)[0]["rounds"][0]["resources"]
    assert resources(samples) == pytest.approx((10, 100))
    invalid = copy.deepcopy(samples[0])
    invalid["at"] = 1500
    invalid["processes"][0]["cpu_seconds"] = float("nan")
    with pytest.raises(ValueError, match="invalid"):
        resources([samples[0], invalid, samples[1]])
    duplicate = copy.deepcopy(samples)
    duplicate[0]["processes"] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        resources(duplicate)


def test_cli_missing_report_produces_a_failed_evidence_report(tmp_path, monkeypatch) -> None:
    output = tmp_path / "comparison.json"
    absent = str(tmp_path / "absent.json")
    monkeypatch.setattr(
        "sys.argv",
        [
            "compare_display_reports",
            "--baseline",
            absent,
            "--candidate",
            absent,
            "--output",
            str(output),
        ],
    )
    main()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["performance_gate_passed"] is False
    assert "incomplete" in result["conclusion"]
