"""每秒彙總：依目標統計送出、完成、丟棄與延遲，轉成 web 回報內容與終端機輸出。

延遲與 web 儀表板的「平均回應時間」同一個定義（README 第 6 節）：下單與追蹤的每一筆 ok、timeout 都計入，
只送總和與筆數，web 端算出的平均是精確值。
"""

OUTCOMES = ("ok", "timeout", "busy", "error")
LATENCY_OUTCOMES = ("ok", "timeout")


class Window:
    __slots__ = ("sent", "orders", "dropped", "counts", "latency_sum_ms", "latency_count", "instance_ids")

    def __init__(self):
        self.sent = 0  # 送出的請求（下單與追蹤）
        self.orders = 0  # 其中的下單請求
        self.dropped = 0
        self.counts = dict.fromkeys(OUTCOMES, 0)
        self.latency_sum_ms = 0.0  # ok 與 timeout 的延遲總和
        self.latency_count = 0
        self.instance_ids: set[str] = set()

    @property
    def failed(self) -> int:
        return self.counts["timeout"] + self.counts["busy"] + self.counts["error"]

    @property
    def avg_ms(self) -> float | None:
        return self.latency_sum_ms / self.latency_count if self.latency_count else None


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
            m.latency_sum_ms += w.latency_sum_ms
            m.latency_count += w.latency_count
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
        if outcome in LATENCY_OUTCOMES:
            w.latency_sum_ms += latency_ms
            w.latency_count += 1
        if instance_id:
            w.instance_ids.add(instance_id)

    def flush(self) -> dict[str, Window]:
        windows, self._windows = self._windows, {t: Window() for t in self.targets}
        return windows


def report_payload(windows: dict[str, Window]) -> dict:
    """POST /api/reports/loadtest 的內容；dropped 只顯示在終端機，不回報。"""
    return {
        "targets": {
            name: {
                **w.counts,
                "latency_sum_ms": round(w.latency_sum_ms, 1),
                "latency_count": w.latency_count,
                "instance_ids": sorted(w.instance_ids),
            }
            for name, w in windows.items()
        }
    }


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
            f"進行中 {inflight.get(name, 0)} 配送中 {delivering.get(name, 0)} 平均回應時間 {format_ms(w.avg_ms)}"
        )
    return " | ".join(parts)


def total_lines(totals: dict[str, Window], abandoned: dict[str, int], gave_up: dict[str, int] | None = None) -> list[str]:
    gave_up = gave_up or {}
    return [
        f"{name} 共下單 {w.orders} 請求 {w.sent} 成功 {w.counts['ok']} 逾時 {w.counts['timeout']} "
        f"忙碌 {w.counts['busy']} 錯誤 {w.counts['error']} 丟棄 {w.dropped} "
        f"放棄下單 {gave_up.get(name, 0)} 放棄追蹤 {abandoned.get(name, 0)}"
        for name, w in totals.items()
    ]
