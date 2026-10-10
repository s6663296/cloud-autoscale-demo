"""Debug 儀表板指標（README 第 6 節）。不依賴 FastAPI，時鐘由呼叫端注入。

以「目標 × 來源」為維度，用 600 格的環狀陣列保存每秒資料。
資料一律以 web 收到的時間分格，不使用回報方的時間戳。

所有指標都以整個流程計算：下單與追蹤（更新外送員位置）都算。
平均回應時間收 ok 與 timeout（逾時以 10000 毫秒計）；busy 與 error 很快就回來，
算進延遲會讓服務越爛、延遲看起來越好，改由成功率呈現。
"""

import time
from typing import Callable

TARGETS = ("fixed", "auto")
SOURCES = ("student", "loadtest")
OUTCOMES = ("ok", "timeout", "busy", "error")
LATENCY_OUTCOMES = ("ok", "timeout")

HISTORY_S = 600
WINDOW_S = 10  # 卡片上所有指標共用的視窗，與趨勢圖每一點的長度相同
SERIES_STEP_S = 10
TIMEOUT_LATENCY_MS = 10000


class _Cell:
    __slots__ = ("counts", "latency_sum_ms", "latency_count")

    def __init__(self):
        self.counts = dict.fromkeys(OUTCOMES, 0)
        self.latency_sum_ms = 0.0
        self.latency_count = 0


class _Slot:
    __slots__ = ("sec", "cells", "instances")

    def __init__(self, sec: int):
        self.sec = sec
        self.cells = {(t, s): _Cell() for t in TARGETS for s in SOURCES}
        self.instances: dict[str, set[str]] = {t: set() for t in TARGETS}


class Metrics:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._slots: list[_Slot | None] = [None] * HISTORY_S
        self._series_cache: dict[int, dict] = {}

    # --- 寫入 ---------------------------------------------------------------

    def record_order(self, results: list[dict]) -> None:
        """觀眾訂單：一筆訂單在兩個目標的派單結果。"""
        slot = self._current_slot()
        for r in results:
            self._record_student(slot, r)

    def record_track(self, result: dict) -> None:
        """觀眾手機的單次追蹤結果，與下單一樣計入所有指標。"""
        self._record_student(self._current_slot(), result)

    @staticmethod
    def _record_student(slot: "_Slot", r: dict) -> None:
        cell = slot.cells[(r["target"], "student")]
        cell.counts[r["outcome"]] += 1
        if r["outcome"] in LATENCY_OUTCOMES:
            cell.latency_sum_ms += TIMEOUT_LATENCY_MS if r["outcome"] == "timeout" else r["latency_ms"]
            cell.latency_count += 1
        if r.get("instance_id"):
            slot.instances[r["target"]].add(r["instance_id"])

    def record_loadtest(self, targets: dict[str, dict]) -> None:
        """壓力測試每秒彙總；延遲已由壓測端加總（只含 ok 與 timeout）。"""
        slot = self._current_slot()
        for target, data in targets.items():
            cell = slot.cells[(target, "loadtest")]
            for outcome in OUTCOMES:
                cell.counts[outcome] += data.get(outcome, 0)
            cell.latency_sum_ms += data.get("latency_sum_ms", 0.0)
            cell.latency_count += data.get("latency_count", 0)
            slot.instances[target].update(data.get("instance_ids", []))

    def _current_slot(self) -> _Slot:
        sec = int(self._clock())
        i = sec % HISTORY_S
        slot = self._slots[i]
        if slot is None or slot.sec != sec:
            slot = self._slots[i] = _Slot(sec)
        return slot

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
    def _summary(slots: list[_Slot], target: str) -> dict:
        """成功率與平均回應時間；卡片與趨勢圖共用同一套規則。"""
        counts = dict.fromkeys(OUTCOMES, 0)
        latency_sum, latency_count = 0.0, 0
        for slot in slots:
            for source in SOURCES:
                cell = slot.cells[(target, source)]
                for outcome in OUTCOMES:
                    counts[outcome] += cell.counts[outcome]
                latency_sum += cell.latency_sum_ms
                latency_count += cell.latency_count
        completed = sum(counts.values())
        return {
            "completed": completed,
            "success_rate": counts["ok"] / completed if completed else None,
            "avg_ms": latency_sum / latency_count if latency_count else None,
            "failures": {o: counts[o] for o in ("timeout", "busy", "error")},
        }

    def snapshot(self, now: float | None = None) -> dict:
        """卡片指標：最近 WINDOW_S 個已結束的秒。目前這一秒還在寫入，不採用。"""
        now_sec = int(self._clock() if now is None else now)
        window = self._slots_between(now_sec - WINDOW_S, now_sec)

        targets = {}
        for target in TARGETS:
            s = self._summary(window, target)
            targets[target] = {
                "instances": len(set().union(*(slot.instances[target] for slot in window))),
                "rps": s["completed"] / WINDOW_S,
                "success_rate": s["success_rate"],
                "avg_ms": s["avg_ms"],
                "failures": s["failures"],
            }
        return {"now": now_sec, "targets": targets, "series": self._series(now_sec)}

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
            s = self._summary(slots, target)
            point[target] = {"success_rate": s["success_rate"], "avg_ms": s["avg_ms"]}
        return point
