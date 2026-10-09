"""每秒彙總：依目標統計送出、完成、丟棄與延遲，轉成 web 回報內容與終端機輸出。"""

import math
import random

OUTCOMES = ("ok", "timeout", "busy", "error")
MAX_SAMPLES = 200


class Window:
    __slots__ = ("sent", "dropped", "counts", "latencies", "instance_ids")

    def __init__(self):
        self.sent = 0
        self.dropped = 0
        self.counts = dict.fromkeys(OUTCOMES, 0)
        self.latencies: list[float] = []
        self.instance_ids: set[str] = set()

    @property
    def failed(self) -> int:
        return self.counts["timeout"] + self.counts["busy"] + self.counts["error"]


class Aggregator:
    def __init__(self, targets: list[str]):
        self.targets = list(targets)
        self._windows = {t: Window() for t in self.targets}

    def sent(self, target: str) -> None:
        self._windows[target].sent += 1

    def dropped(self, target: str) -> None:
        self._windows[target].dropped += 1

    def completed(self, target: str, outcome: str, latency_ms: float, instance_id: str | None) -> None:
        w = self._windows[target]
        w.counts[outcome] += 1
        w.latencies.append(latency_ms)
        if instance_id:
            w.instance_ids.add(instance_id)

    def flush(self) -> dict[str, Window]:
        windows, self._windows = self._windows, {t: Window() for t in self.targets}
        return windows


def report_payload(windows: dict[str, Window], rng: random.Random) -> dict:
    """POST /api/reports/loadtest 的內容；dropped 只顯示在終端機，不回報。"""
    targets = {}
    for name, w in windows.items():
        samples = w.latencies if len(w.latencies) <= MAX_SAMPLES else rng.sample(w.latencies, MAX_SAMPLES)
        targets[name] = {
            **w.counts,
            "latency_samples_ms": [round(v, 1) for v in samples],
            "instance_ids": sorted(w.instance_ids),
        }
    return {"targets": targets}


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]


def format_ms(ms: float | None) -> str:
    if ms is None:
        return "-"
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def summary_line(elapsed: float, rate: float, windows: dict[str, Window], inflight: dict[str, int]) -> str:
    parts = [f"[{elapsed:5.0f}s] 速率 {rate:6.1f}/s"]
    for name, w in windows.items():
        parts.append(
            f"{name} 送出 {w.sent} 成功 {w.counts['ok']} 失敗 {w.failed} 丟棄 {w.dropped} "
            f"進行中 {inflight.get(name, 0)} p95 {format_ms(p95(w.latencies))}"
        )
    return " | ".join(parts)
