"""Debug 儀表板指標（README 第 6 節）。不依賴 FastAPI，時鐘由呼叫端注入。

以「目標 × 來源」為維度，用 600 格的環狀陣列保存每秒資料。
資料一律以 hub 收到的時間分格，不使用回報方的時間戳。
"""

import math
import random
import time
from typing import Callable

TARGETS = ("fixed", "auto")
SOURCES = ("student", "loadtest")
OUTCOMES = ("ok", "timeout", "busy", "error")

HISTORY_S = 600
WINDOW_S = 60
INSTANCE_WINDOW_S = 10
SERIES_STEP_S = 10
MAX_SAMPLES = 200
TIMEOUT_LATENCY_MS = 15000


def percentile(samples: list[float], p: float) -> float | None:
    """最近排名法（nearest-rank）。"""
    if not samples:
        return None
    ordered = sorted(samples)
    k = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[k - 1]


class _Cell:
    __slots__ = ("counts", "samples", "seen")

    def __init__(self):
        self.counts = dict.fromkeys(OUTCOMES, 0)
        self.samples: list[float] = []
        self.seen = 0


class _Slot:
    __slots__ = ("sec", "cells", "instances", "student_orders")

    def __init__(self, sec: int):
        self.sec = sec
        self.cells = {(t, s): _Cell() for t in TARGETS for s in SOURCES}
        self.instances: dict[str, set[str]] = {t: set() for t in TARGETS}
        self.student_orders = 0


class Metrics:
    def __init__(self, clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        self._clock = clock
        self._rng = rng or random.Random()
        self._slots: list[_Slot | None] = [None] * HISTORY_S
        self._series_cache: dict[int, dict] = {}

    # --- 寫入 ---------------------------------------------------------------

    def record_order(self, results: list[dict]) -> None:
        """觀眾訂單：一筆訂單在兩個目標的結果。"""
        slot = self._current_slot()
        slot.student_orders += 1
        for r in results:
            target, outcome = r["target"], r["outcome"]
            cell = slot.cells[(target, "student")]
            cell.counts[outcome] += 1
            latency = TIMEOUT_LATENCY_MS if outcome == "timeout" else r["latency_ms"]
            self._add_samples(cell, [latency])
            if r.get("instance_id"):
                slot.instances[target].add(r["instance_id"])

    def record_loadtest(self, targets: dict[str, dict]) -> None:
        """壓力測試每秒彙總。"""
        slot = self._current_slot()
        for target, data in targets.items():
            cell = slot.cells[(target, "loadtest")]
            for outcome in OUTCOMES:
                cell.counts[outcome] += data.get(outcome, 0)
            self._add_samples(cell, data.get("latency_samples_ms", []))
            slot.instances[target].update(data.get("instance_ids", []))

    def _current_slot(self) -> _Slot:
        sec = int(self._clock())
        i = sec % HISTORY_S
        slot = self._slots[i]
        if slot is None or slot.sec != sec:
            slot = self._slots[i] = _Slot(sec)
        return slot

    def _add_samples(self, cell: _Cell, samples: list[float]) -> None:
        """蓄水池抽樣，每格最多保留 MAX_SAMPLES 筆。"""
        for value in samples:
            cell.seen += 1
            if len(cell.samples) < MAX_SAMPLES:
                cell.samples.append(value)
            else:
                j = self._rng.randrange(cell.seen)
                if j < MAX_SAMPLES:
                    cell.samples[j] = value

    # --- 讀取 ---------------------------------------------------------------

    def _slots_between(self, start: int, end: int) -> list[_Slot]:
        """sec 落在 [start, end) 的格子。"""
        found = []
        for sec in range(start, end):
            slot = self._slots[sec % HISTORY_S]
            if slot is not None and slot.sec == sec:
                found.append(slot)
        return found

    @staticmethod
    def _collect(slots: list[_Slot], target: str) -> tuple[dict[str, int], list[float]]:
        counts = dict.fromkeys(OUTCOMES, 0)
        samples: list[float] = []
        for slot in slots:
            for source in SOURCES:
                cell = slot.cells[(target, source)]
                for outcome in OUTCOMES:
                    counts[outcome] += cell.counts[outcome]
                samples.extend(cell.samples)
        return counts, samples

    def samples_in_window(self, target: str, now: float, seconds: int) -> list[float]:
        now_sec = int(now)
        return self._collect(self._slots_between(now_sec - seconds + 1, now_sec + 1), target)[1]

    def snapshot(self, now: float | None = None) -> dict:
        now_sec = int(self._clock() if now is None else now)
        window = self._slots_between(now_sec - WINDOW_S + 1, now_sec + 1)
        recent = self._slots_between(now_sec - INSTANCE_WINDOW_S + 1, now_sec + 1)

        targets = {}
        for target in TARGETS:
            counts, samples = self._collect(window, target)
            completed = sum(counts.values())
            targets[target] = {
                "instances": len(set().union(*(s.instances[target] for s in recent))),
                "rps": completed / WINDOW_S,
                "success_rate": counts["ok"] / completed if completed else None,
                "p50_ms": percentile(samples, 50) if completed else None,
                "p95_ms": percentile(samples, 95) if completed else None,
                "failures": {o: counts[o] for o in ("timeout", "busy", "error")},
            }

        students = {
            "orders": sum(s.student_orders for s in window),
            "fixed_ok": sum(s.cells[("fixed", "student")].counts["ok"] for s in window),
            "auto_ok": sum(s.cells[("auto", "student")].counts["ok"] for s in window),
        }
        return {"now": now_sec, "targets": targets, "students": students, "series": self._series(now_sec)}

    def _series(self, now_sec: int) -> list[dict]:
        """已結束的區間不會再寫入（寫入一律落在目前時間），計算一次後快取。

        區間結束時間須早於 now_sec，以容忍廣播迴圈比整秒略早醒來。
        """
        end = now_sec - now_sec % SERIES_STEP_S + SERIES_STEP_S
        first = end - HISTORY_S
        self._series_cache = {t: p for t, p in self._series_cache.items() if t >= first}
        points = []
        for start in range(first, end, SERIES_STEP_S):
            point = self._series_cache.get(start)
            if point is None:
                point = self._series_point(start)
                if start + SERIES_STEP_S < now_sec:
                    self._series_cache[start] = point
            points.append(point)
        return points

    def _series_point(self, start: int) -> dict:
        slots = self._slots_between(start, start + SERIES_STEP_S)
        point = {"t": start}
        for target in TARGETS:
            counts, samples = self._collect(slots, target)
            completed = sum(counts.values())
            point[target] = {
                "success_rate": counts["ok"] / completed if completed else None,
                "p95_ms": percentile(samples, 95) if completed else None,
            }
        return point
