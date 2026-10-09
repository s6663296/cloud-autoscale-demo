"""每秒彙總：依目標統計送出、完成、丟棄與延遲，轉成 web 回報內容與終端機輸出。"""

import math
import random

OUTCOMES = ("ok", "timeout", "busy", "error")
MAX_SAMPLES = 200


class Window:
    __slots__ = ("sent", "orders", "dropped", "counts", "latencies", "instance_ids")

    def __init__(self):
        self.sent = 0  # 送出的請求（下單與追蹤）
        self.orders = 0  # 其中的下單請求
        self.dropped = 0
        self.counts = dict.fromkeys(OUTCOMES, 0)
        self.latencies: list[float] = []
        self.instance_ids: set[str] = set()

    @property
    def failed(self) -> int:
        return self.counts["timeout"] + self.counts["busy"] + self.counts["error"]


def merge_windows(parts: list[dict[str, Window]]) -> dict[str, Window]:
    """把多個程序同一秒（或同一段期間）的統計合併成一份。"""
    merged: dict[str, Window] = {}
    for windows in parts:
        for name, w in windows.items():
            m = merged.setdefault(name, Window())
            m.sent += w.sent
            m.orders += w.orders
            m.dropped += w.dropped
            for outcome, n in w.counts.items():
                m.counts[outcome] += n
            m.latencies.extend(w.latencies)
            m.instance_ids |= w.instance_ids
    return merged


class Aggregator:
    def __init__(self, targets: list[str]):
        self.targets = list(targets)
        self._windows = {t: Window() for t in self.targets}

    def sent(self, target: str, order: bool = False) -> None:
        w = self._windows[target]
        w.sent += 1
        if order:
            w.orders += 1

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


def summary_line(
    elapsed: float, rate: float, windows: dict[str, Window], inflight: dict[str, int], delivering: dict[str, int]
) -> str:
    parts = [f"[{elapsed:5.0f}s] 新顧客 {rate:6.1f}/s"]
    for name, w in windows.items():
        parts.append(
            f"{name} 下單 {w.orders} 請求 {w.sent} 成功 {w.counts['ok']} 失敗 {w.failed} 丟棄 {w.dropped} "
            f"進行中 {inflight.get(name, 0)} 配送中 {delivering.get(name, 0)} p95 {format_ms(p95(w.latencies))}"
        )
    return " | ".join(parts)


def total_lines(totals: dict[str, Window], abandoned: dict[str, int]) -> list[str]:
    return [
        f"{name} 共下單 {w.orders} 請求 {w.sent} 成功 {w.counts['ok']} 逾時 {w.counts['timeout']} "
        f"忙碌 {w.counts['busy']} 錯誤 {w.counts['error']} 丟棄 {w.dropped} 放棄追蹤 {abandoned.get(name, 0)}"
        for name, w in totals.items()
    ]
