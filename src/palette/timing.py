"""Small in-memory latency windows; never retain input or image payloads."""

from __future__ import annotations

import math
from collections import deque


class TimingWindow:
    def __init__(self) -> None:
        self.samples: dict[str, deque[float]] = {}

    def record(self, **milliseconds: float) -> None:
        for name, value in milliseconds.items():
            if math.isfinite(value):
                self.samples.setdefault(name, deque(maxlen=256)).append(max(0.0, value))

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        result: dict[str, dict[str, float | int]] = {}
        for name, samples in self.samples.items():
            values = sorted(samples)
            result[name] = {
                "count": len(values),
                **{
                    f"p{percentile}": round(
                        values[math.ceil(len(values) * percentile / 100) - 1], 3
                    )
                    for percentile in (50, 95, 99)
                },
            }
        return result


def process_tree_metrics() -> dict[str, object]:
    """Cumulative CPU seconds and RSS, with identities for honest interval deltas."""
    import psutil

    owner = psutil.Process()
    processes = []
    incomplete = False
    try:
        children = owner.children(recursive=True)
    except psutil.Error:
        children = []
        incomplete = True
    for process in [owner, *children]:
        try:
            with process.oneshot():
                cpu = process.cpu_times()
                processes.append(
                    {
                        "pid": process.pid,
                        "created_at": process.create_time(),
                        "cpu_seconds": cpu.user + cpu.system,
                        "rss_bytes": process.memory_info().rss,
                    }
                )
        except psutil.Error:
            incomplete = True
    return {"processes": processes, "incomplete": incomplete}
