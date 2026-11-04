"""Validate real Desktop reports for steady VNC refresh or the raw display experiment."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from itertools import pairwise
from pathlib import Path
from typing import Any

MATRIX = {(1280, 800), (1920, 1080)}


def p95(values: list[float], minimum: int = 100) -> float:
    if len(values) < minimum or any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("insufficient or invalid measurements")
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


def resources(samples: list[dict[str, Any]]) -> tuple[float, float]:
    if len(samples) < 2 or any(sample["incomplete"] for sample in samples):
        raise ValueError("resource coverage is incomplete")
    first, last = samples[0], samples[-1]

    def identities(sample: dict[str, Any]) -> set[tuple[int, float]]:
        return {(item["pid"], item["created_at"]) for item in sample["processes"]}

    for sample in samples:
        if (
            not math.isfinite(sample["at"])
            or len(identities(sample)) != len(sample["processes"])
            or any(
                not math.isfinite(item[field]) or item[field] < 0
                for item in sample["processes"]
                for field in ("cpu_seconds", "rss_bytes")
            )
        ):
            raise ValueError("invalid or duplicate process resource samples")
    if any(current["at"] <= previous["at"] for previous, current in pairwise(samples)):
        raise ValueError("resource sample times must increase")
    if any(identities(sample) != identities(first) for sample in samples):
        raise ValueError("process membership changed during resource measurement")
    elapsed = (last["at"] - first["at"]) / 1000
    cpu = sum(item["cpu_seconds"] for item in last["processes"]) - sum(
        item["cpu_seconds"] for item in first["processes"]
    )
    rss = max(sum(item["rss_bytes"] for item in sample["processes"]) for sample in samples)
    if (
        elapsed <= 0
        or cpu < 0
        or rss <= 0
        or not all(math.isfinite(value) for value in (elapsed, cpu, rss))
    ):
        raise ValueError("invalid resource samples")
    return cpu / elapsed * 100, float(rss)


def variant(reports: list[dict[str, Any]]) -> str:
    variants = {
        r.get("variant", "raw" if r.get("provider") == "raw" else "adaptive") for r in reports
    }
    if len(variants) != 1:
        raise ValueError("each side must contain one display variant")
    result = variants.pop()
    if result not in {"adaptive", "steady", "raw"}:
        raise ValueError("unknown display variant")
    return str(result)


def pipeline(reports: list[dict[str, Any]]) -> str:
    values = {report.get("observation_pipeline", "unspecified") for report in reports}
    if len(values) != 1:
        raise ValueError("each side must use one observation pipeline")
    result = values.pop()
    if result not in {"serial", "split"} and any(
        report.get("schema_version", 1) >= 2 for report in reports
    ):
        raise ValueError("observation pipeline metadata is missing or invalid")
    return str(result)


def matched_motion(motion: dict[str, Any]) -> float:
    issued, matched = motion["issued"], motion["samples"]
    if len(issued) < 100:
        raise ValueError("at least 100 dispatched motion inputs are required")
    by_id = {item["id"]: item for item in issued}
    if len(by_id) != len(issued) or len({item["id"] for item in matched}) != len(matched):
        raise ValueError("motion input identifiers must be unique")
    timings = []
    for item in matched:
        original = by_id.get(item["id"])
        if (
            original is None
            or item["input_at"] != original["at"]
            or not math.isclose(item["latency_ms"], item["visible_at"] - item["input_at"])
        ):
            raise ValueError("motion feedback is not matched to its dispatched input")
        timings.append(item["latency_ms"])
    if timings != motion["input_to_visible_ms"] or motion["merged_or_not_presented"] != len(
        issued
    ) - len(matched):
        raise ValueError("inconsistent motion matching counts or timings")
    last = next((item for item in matched if item["id"] == issued[-1]["id"]), None)
    if last is None or not math.isclose(motion["settle_ms"], last["latency_ms"]):
        raise ValueError("final motion target was not observed")
    return p95(timings, 10)


def collect(
    reports: list[dict[str, Any]], expected_variant: str
) -> dict[tuple[int, int, str], dict[str, float]]:
    rows: dict[tuple[int, int, str], dict[str, float]] = {}
    if len(reports) != 2 or {(r["width"], r["height"]) for r in reports} != MATRIX:
        raise ValueError("both 1280x800 and 1920x1080 are required")
    provider = "raw" if expected_variant == "raw" else "rfb"
    for report in reports:
        if (
            report.get("kind") != "desktop-display"
            or report.get("provider") != provider
            or report.get("complete") is not True
        ):
            raise ValueError("only complete Desktop reports from the expected provider qualify")
        detailed = report.get("schema_version", 1) >= 2
        if expected_variant == "steady" and not detailed:
            raise ValueError(
                "steady refresh requires schema 2 matched motion and idle measurements"
            )
        if detailed:
            protocol = report["protocol"]
            active = [
                item
                for item in report["display_settings"]["instances"]
                if item["vm_id"] == report["vm_id"] and item["instance_id"] == report["instance_id"]
            ]
            if (
                len(active) != 1
                or active[0]["provider"] != provider
                or active[0]["refresh_mode"]
                != ("adaptive" if expected_variant == "adaptive" else "steady")
            ):
                raise ValueError("actual display mode does not match report variant")
            if (
                protocol["samples"] < 100
                or protocol["rounds"] < 3
                or set(protocol["idle_seconds"]) != {10, 120}
                or protocol["wake_samples"] < 1
                or not 1 <= protocol["motion_hz"] <= 125
            ):
                raise ValueError(
                    "full protocol requires 100 samples, three rounds and 10/120s idle"
                )
        for load in ("idle", "observe"):
            rounds = [row for row in report["rounds"] if row["load"] == load]
            if len(rounds) < 3 or len({row["round"] for row in rounds}) != len(rounds):
                raise ValueError("at least three distinct rounds are required for each load")
            if detailed and {wake["input_kind"] for row in rounds for wake in row["idle_wake"]} != {
                "key",
                "drag",
                "scroll",
            }:
                raise ValueError("idle feedback must cover key, drag and scroll")
            names = ["visible", "ack", "observe", "cpu", "rss", "drag_frame", "scroll_frame"]
            if detailed:
                names += [
                    "drag_visible",
                    "scroll_visible",
                    "drag_settle",
                    "scroll_settle",
                    "wake_10",
                    "wake_120",
                    "idle_cpu_10",
                    "idle_cpu_120",
                    "idle_rss_10",
                    "idle_rss_120",
                ]
            if provider == "raw":
                names.append("shadow")
            measurements: dict[str, list[float]] = {key: [] for key in names}
            for row in rounds:
                for metric, key in (
                    ("visible", "input_to_visible_ms"),
                    ("ack", "input_to_ack_ms"),
                    ("observe", "observation_ms"),
                ):
                    measurements[metric].append(p95(row[key]))
                for phase in ("drag", "scroll"):
                    measurements[f"{phase}_frame"].append(
                        p95(row[f"{phase}_frame_intervals_ms"], 10)
                    )
                    if detailed:
                        motion = row[phase]
                        measurements[f"{phase}_visible"].append(matched_motion(motion))
                        measurements[f"{phase}_settle"].append(p95([motion["settle_ms"]], 1))
                if detailed:
                    if len(row["numbered_inputs"]) < 100:
                        raise ValueError("at least 100 numbered inputs are required")
                    for seconds in (10, 120):
                        wake = [
                            item for item in row["idle_wake"] if item["idle_seconds"] == seconds
                        ]
                        if len(wake) < protocol["wake_samples"]:
                            raise ValueError("missing idle first-feedback measurements")
                        if any(
                            item["resources"][-1]["at"] - item["resources"][0]["at"] < seconds * 990
                            for item in wake
                        ):
                            raise ValueError("idle interval was shorter than declared")
                        measurements[f"wake_{seconds}"].append(
                            p95([item["latency_ms"] for item in wake], 1)
                        )
                        idle = [resources(item["resources"]) for item in wake]
                        measurements[f"idle_cpu_{seconds}"].append(
                            statistics.mean(item[0] for item in idle)
                        )
                        measurements[f"idle_rss_{seconds}"].append(max(item[1] for item in idle))
                if provider == "raw":
                    measurements["shadow"].append(p95(row["shadow_observation_ms"]))
                cpu, rss = resources(row["resources"])
                measurements["cpu"].append(cpu)
                measurements["rss"].append(rss)
            rows[(report["width"], report["height"], load)] = {
                key: statistics.median(value) for key, value in measurements.items()
            }
    return rows


def compare(baseline: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> dict[str, Any]:
    all_reports = baseline + candidate
    if not all_reports:
        raise ValueError("no reports supplied")
    for field in ("host", "guest", "guest_arch", "accelerator", "execution"):
        if not all_reports[0].get(field):
            raise ValueError(f"execution metadata is missing: {field}")
        if any(report.get(field) != all_reports[0].get(field) for report in all_reports):
            raise ValueError(f"execution metadata differs: {field}")
    old_variant, new_variant = variant(baseline), variant(candidate)
    old_pipeline, new_pipeline = pipeline(baseline), pipeline(candidate)
    if old_variant != "adaptive" or new_variant not in {"steady", "raw"}:
        raise ValueError("compare adaptive VNC against steady VNC or raw")
    detailed = all(r.get("schema_version", 1) >= 2 for r in all_reports)
    if new_variant == "steady" and not detailed:
        raise ValueError("steady refresh requires schema 2 evidence on both sides")
    if new_variant == "steady" and new_pipeline != "split":
        raise ValueError("steady-refresh candidate must use the split observation pipeline")
    if detailed and any(r["protocol"] != all_reports[0]["protocol"] for r in all_reports):
        raise ValueError("benchmark protocols differ")
    old, new = collect(baseline, old_variant), collect(candidate, new_variant)
    before, after = old[(1920, 1080, "observe")], new[(1920, 1080, "observe")]
    if new_variant == "steady":
        improved = all(
            after[f"{phase}_visible"] <= before[f"{phase}_visible"] * 0.8
            for phase in ("drag", "scroll")
        )
    else:
        improved = (
            after["visible"] <= before["visible"] * 0.8
            or after["shadow"] <= before["observe"] * 0.8
        )
    checked = ["visible", "ack", "observe", "cpu", "rss", "drag_frame", "scroll_frame"]
    if detailed:
        checked += [
            "drag_visible",
            "scroll_visible",
            "drag_settle",
            "scroll_settle",
            "wake_10",
            "wake_120",
        ]
    regressions = [
        {"width": key[0], "height": key[1], "load": key[2], "metric": metric}
        for key in old
        for metric in checked
        if new[key][metric] > old[key][metric] * 1.1
    ]
    passed = improved and not regressions
    combined = new_variant == "steady" and old_pipeline == "serial" and new_pipeline == "split"
    return {
        "schema_version": 2,
        "baseline_variant": old_variant,
        "candidate_variant": new_variant,
        "baseline_observation_pipeline": old_pipeline,
        "candidate_observation_pipeline": new_pipeline,
        "comparison_scope": "display_and_observation"
        if combined
        else "display_refresh_only"
        if new_variant == "steady"
        else "raw_display_experiment",
        "performance_gate_passed": passed,
        "combined_optimization_gate_passed": passed and combined,
        "primary_improvement_passed": improved,
        "regressions": regressions,
        "conclusion": (
            "eligible for local steady-refresh enablement"
            if new_variant == "steady"
            else "eligible for further migration review"
        )
        if passed
        else "retain the existing display mode",
        "migration_recommended": False,
        "limitations": [
            "Shadow observation is not an authoritative action frame.",
            "Idle CPU/RSS are reported separately; the 10% active-resource gate excludes static wake intervals.",
            "Canvas visibility is not physical monitor presentation.",
            "This performance gate does not replace resize, ownership, disconnect, crash and endurance acceptance.",
        ],
        "matrix": [
            {
                "width": key[0],
                "height": key[1],
                "load": key[2],
                "baseline": old[key],
                "candidate": new[key],
            }
            for key in sorted(old)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, nargs="+", required=True)
    parser.add_argument("--candidate", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compare(
            [json.loads(p.read_text(encoding="utf-8")) for p in args.baseline],
            [json.loads(p.read_text(encoding="utf-8")) for p in args.candidate],
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result = {
            "performance_gate_passed": False,
            "migration_recommended": False,
            "conclusion": "retain existing display; acceptance evidence is incomplete",
            "error": str(error),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
