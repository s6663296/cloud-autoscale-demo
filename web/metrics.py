"""Debug 儀表板指標（README 第 6 節）。不依賴 FastAPI，時鐘由呼叫端注入。

以「目標 × 來源」為維度，用 600 格的環狀陣列保存每秒資料。
資料一律以 web 收到的時間分格，不使用回報方的時間戳。

所有指標都以整個流程計算：下單與追蹤（更新外送員位置）都算。
平均回應時間收 ok 與 timeout（逾時以 10000 毫秒計）；busy 與 error 很快就回來，
算進延遲會讓服務越爛、延遲看起來越好，改由成功率呈現。
"""

import time
import unicodedata
from typing import Callable

TARGETS = ("fixed", "auto")
SOURCES = ("student", "loadtest")
OUTCOMES = ("ok", "timeout", "busy", "error")
LATENCY_OUTCOMES = ("ok", "timeout")

HISTORY_S = 600
WINDOW_S = 10  # 卡片上所有指標共用的視窗，與趨勢圖每一點的長度相同
SERIES_STEP_S = 10
TIMEOUT_LATENCY_MS = 10000
NEW_MACHINE_S = 30  # 第一次出現在這段時間內的機器標「新開機」
IDLE_MACHINE_S = 60  # 最近 10 秒沒接單、但這段時間內接過的機器標「閒置」
MAX_PHONES = 20  # 每台機器在 snapshot 中最多列出的暱稱數
MAX_REQUEST_EVENTS = 200  # 每邊每秒最多保留的手機請求筆數（畫飛行的小點）
NICKNAME_MAX = 12
GUEST_NAME = "訪客"


def clean_nickname(name: str) -> str:
    """去掉控制字元與頭尾空白、截到 12 個字；空的當成「訪客」。不改其他內容，前端一律以純文字顯示。"""
    text = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip()
    return text[:NICKNAME_MAX] or GUEST_NAME


class _Cell:
    __slots__ = ("counts", "latency_sum_ms", "latency_count")

    def __init__(self):
        self.counts = dict.fromkeys(OUTCOMES, 0)
        self.latency_sum_ms = 0.0
        self.latency_count = 0


class _Machine:
    """一台機器在某一秒處理過的手機與模擬顧客筆數。"""

    __slots__ = ("clients", "loadtest", "loadtest_unknown")

    def __init__(self):
        self.clients: dict[str, str] = {}  # client_id → 暱稱
        self.loadtest = 0
        self.loadtest_unknown = False  # 舊版壓測只送 instance_ids，沒有每台的筆數


class _Slot:
    __slots__ = ("sec", "cells", "instances", "machines", "requests")

    def __init__(self, sec: int):
        self.sec = sec
        self.cells = {(t, s): _Cell() for t in TARGETS for s in SOURCES}
        self.instances: dict[str, set[str]] = {t: set() for t in TARGETS}
        self.machines: dict[str, dict[str, _Machine]] = {t: {} for t in TARGETS}
        self.requests: dict[str, list[dict]] = {t: [] for t in TARGETS}


class Metrics:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._slots: list[_Slot | None] = [None] * HISTORY_S
        self._series_cache: dict[int, dict] = {}
        self._first_seen: dict[str, dict[str, int]] = {t: {} for t in TARGETS}
        self._last_seen: dict[str, dict[str, int]] = {t: {} for t in TARGETS}

    # --- 寫入 ---------------------------------------------------------------

    def record_order(self, results: list[dict], client: dict | None = None) -> None:
        """觀眾訂單：一筆訂單在兩個目標的派單結果。"""
        slot = self._current_slot()
        for r in results:
            self._record_student(slot, r, client)

    def record_track(self, result: dict, client: dict | None = None) -> None:
        """觀眾手機的單次追蹤結果，與下單一樣計入所有指標。"""
        self._record_student(self._current_slot(), result, client)

    def _record_student(self, slot: "_Slot", r: dict, client: dict | None) -> None:
        cell = slot.cells[(r["target"], "student")]
        cell.counts[r["outcome"]] += 1
        if r["outcome"] in LATENCY_OUTCOMES:
            cell.latency_sum_ms += TIMEOUT_LATENCY_MS if r["outcome"] == "timeout" else r["latency_ms"]
            cell.latency_count += 1
        name = clean_nickname(client.get("name", "")) if client else None
        if r.get("instance_id"):
            machine = self._machine(slot, r["target"], r["instance_id"])
            if client:
                machine.clients[client["id"]] = name
        if client:
            events = slot.requests[r["target"]]
            if len(events) < MAX_REQUEST_EVENTS:
                events.append({"tag": client["id"][:8], "name": name, "instance": r.get("instance_id"),
                               "outcome": r["outcome"]})

    def record_loadtest(self, targets: dict[str, dict]) -> None:
        """壓力測試每秒彙總；延遲已由壓測端加總（只含 ok 與 timeout）。"""
        slot = self._current_slot()
        for target, data in targets.items():
            cell = slot.cells[(target, "loadtest")]
            for outcome in OUTCOMES:
                cell.counts[outcome] += data.get(outcome, 0)
            cell.latency_sum_ms += data.get("latency_sum_ms", 0.0)
            cell.latency_count += data.get("latency_count", 0)
            counts = data.get("instance_counts") or {}
            for instance_id in set(data.get("instance_ids", [])) | set(counts):
                machine = self._machine(slot, target, instance_id)
                if instance_id in counts:
                    machine.loadtest += counts[instance_id]
                else:
                    machine.loadtest_unknown = True

    def _machine(self, slot: "_Slot", target: str, instance_id: str) -> "_Machine":
        slot.instances[target].add(instance_id)
        self._first_seen[target].setdefault(instance_id, slot.sec)
        self._last_seen[target][instance_id] = slot.sec
        return slot.machines[target].setdefault(instance_id, _Machine())

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
        self._forget_old_machines(now_sec)
        window = self._slots_between(now_sec - WINDOW_S, now_sec)

        targets = {}
        for target in TARGETS:
            s = self._summary(window, target)
            machines, labels = self._machines(now_sec, target, window)
            last = self._slots_between(now_sec - 1, now_sec)
            targets[target] = {
                "instances": len(set().union(*(slot.instances[target] for slot in window))),
                "rps": s["completed"] / WINDOW_S,
                "success_rate": s["success_rate"],
                "avg_ms": s["avg_ms"],
                "failures": s["failures"],
                "machines": machines,
                "requests": [
                    {"tag": e["tag"], "name": e["name"], "outcome": e["outcome"],
                     "machine": labels.get(e["instance"]) if e["instance"] else None}
                    for slot in last for e in slot.requests[target]
                ],
            }
        return {"now": now_sec, "targets": targets, "series": self._series(now_sec)}

    def _forget_old_machines(self, now_sec: int) -> None:
        for target in TARGETS:
            stale = [i for i, sec in self._last_seen[target].items() if sec < now_sec - HISTORY_S]
            for instance_id in stale:
                del self._last_seen[target][instance_id]
                self._first_seen[target].pop(instance_id, None)

    def _machines(self, now_sec: int, target: str, window: list[_Slot]) -> tuple[list[dict], dict[str, str]]:
        """請求流向圖的機器節點：最近 10 秒有接單的機器，加上 60 秒內接過、現在閒置的機器。"""
        active: dict[str, dict] = {}
        for slot in window:
            for instance_id, m in slot.machines[target].items():
                a = active.setdefault(instance_id, {"clients": {}, "loadtest": 0, "unknown": False})
                a["clients"].update(m.clients)
                a["loadtest"] += m.loadtest
                a["unknown"] = a["unknown"] or m.loadtest_unknown
        recent = self._slots_between(now_sec - IDLE_MACHINE_S, now_sec)
        seen = set().union(*(slot.instances[target] for slot in recent)) if recent else set()
        first_seen = self._first_seen[target]
        ids = sorted(seen, key=lambda i: (first_seen.get(i, now_sec), i))
        labels = _short_ids(ids)

        machines = []
        for instance_id in ids:
            a = active.get(instance_id)
            if a is None:
                status, clients, loadtest = "idle", {}, 0
            else:
                status = "new" if first_seen.get(instance_id, now_sec) >= now_sec - NEW_MACHINE_S else "active"
                clients, loadtest = a["clients"], None if a["unknown"] else a["loadtest"]
            phones = sorted(({"name": name, "tag": cid[:8]} for cid, name in clients.items()),
                            key=lambda p: (p["name"], p["tag"]))
            machines.append({
                "id": labels[instance_id],
                "status": status,
                "phones": phones[:MAX_PHONES],
                "phone_count": len(clients),
                "loadtest_requests": loadtest,
            })
        return machines, labels

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
            point[target] = {
                "success_rate": s["success_rate"],
                "avg_ms": s["avg_ms"],
                "instances": self._instance_count(slots, target, s["completed"]),
            }
        return point

    @staticmethod
    def _instance_count(slots: list[_Slot], target: str, completed: int) -> int | None:
        """不同 instance_id 數；有請求卻一個編號都沒有（全部失敗）時為 None，避免看起來像機器不見了。"""
        ids = set().union(*(slot.instances[target] for slot in slots)) if slots else set()
        if ids:
            return len(ids)
        return None if completed else 0


def _short_ids(ids: list[str]) -> dict[str, str]:
    """畫面上的機器編號：末 4 碼；同一邊有重複時改用末 8 碼。"""
    short = {i: i[-4:] for i in ids}
    if len(set(short.values())) < len(ids):
        short = {i: i[-8:] for i in ids}
    return short
