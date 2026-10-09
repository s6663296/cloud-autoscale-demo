"""壓力測試執行：模擬顧客（下單後追蹤到送達）、每秒回報 web、probe 容量量測（README 第 7 節）。"""

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from loadtest.orders import OrderFactory
from loadtest.schedule import rate_at, run_schedule
from loadtest.stats import Aggregator, Window, report_payload, summary_line

MAX_INFLIGHT = 2000
REQUEST_TIMEOUT_S = 15.0
TIMEOUT_LATENCY_MS = 15000
REPORT_TIMEOUT_S = 3.0
TRACK_INTERVAL_S = 2.0
MAX_TRACK_FAILURES = 5  # 連續失敗這麼多次就放棄追蹤，像真實顧客關掉 App
# httpcore 每個請求都會掃描整個連線池（O(連線數)），連線多時把池子拆小、輪流使用
POOL_SHARDS = 8


@dataclass
class Result:
    outcome: str
    latency_ms: float
    instance_id: str | None = None
    data: Any = None


async def send_request(
    client: httpx.AsyncClient, url: str, path: str, body: dict, timeout_s: float = REQUEST_TIMEOUT_S
) -> Result:
    """送出一個請求並依 README 分類，方式與手機端一致。"""
    start = time.perf_counter()
    try:
        res = await asyncio.wait_for(client.post(f"{url}{path}", json=body), timeout_s)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return Result("timeout", TIMEOUT_LATENCY_MS)
    except httpx.HTTPError:
        return Result("error", (time.perf_counter() - start) * 1000)
    latency = (time.perf_counter() - start) * 1000
    if res.status_code == 200:
        try:
            data = res.json()
        except ValueError:
            return Result("error", latency)
        return Result("ok", latency, data.get("instance_id"), data)
    return Result("busy" if res.status_code == 429 else "error", latency)


class Session:
    """管理各目標的連線、進行中請求、模擬顧客、每秒彙總與回報。"""

    def __init__(
        self,
        targets: dict[str, str],
        web_url: str,
        *,
        stop: asyncio.Event,
        out: Callable[[str], None],
        max_inflight: int = MAX_INFLIGHT,
        timeout_s: float = REQUEST_TIMEOUT_S,
        track_interval_s: float = TRACK_INTERVAL_S,
        target_transport: httpx.AsyncBaseTransport | None = None,
        web_transport: httpx.AsyncBaseTransport | None = None,
        on_complete: Callable[[str, str], None] | None = None,
    ):
        self.targets = {name: url.rstrip("/") for name, url in targets.items()}
        self.web_url = web_url.rstrip("/")
        self.stop = stop
        self.out = out
        self.max_inflight = max_inflight
        self.timeout_s = timeout_s
        self.track_interval_s = track_interval_s
        self.on_complete = on_complete
        self.rate_fn: Callable[[float], float] = lambda elapsed: 0.0
        self.agg = Aggregator(list(self.targets))
        self.inflight = dict.fromkeys(self.targets, 0)
        self.delivering = dict.fromkeys(self.targets, 0)
        self.abandoned = dict.fromkeys(self.targets, 0)
        self.totals = {name: Window() for name in self.targets}
        self.rng = random.Random()
        self._customers: set[asyncio.Task] = set()
        self._reports: set[asyncio.Task] = set()
        self._transports = (target_transport, web_transport)

    async def __aenter__(self) -> "Session":
        target_transport, web_transport = self._transports
        per_shard = max(1, -(-self.max_inflight // POOL_SHARDS))
        limits = httpx.Limits(max_connections=per_shard, max_keepalive_connections=per_shard)
        self.clients = {
            name: [
                httpx.AsyncClient(limits=limits, timeout=self.timeout_s + 5, transport=target_transport)
                for _ in range(POOL_SHARDS)
            ]
            for name in self.targets
        }
        self._next_shard = 0
        self.web = httpx.AsyncClient(timeout=REPORT_TIMEOUT_S, transport=web_transport)
        self.loop = asyncio.get_running_loop()
        self.start = self.loop.time()
        self._reporter = asyncio.create_task(self._report_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        self._reporter.cancel()
        for task in list(self._customers):
            task.cancel()
        await asyncio.gather(self._reporter, *self._customers, return_exceptions=True)
        self._flush()  # 最後一筆回報
        await asyncio.gather(*self._reports, return_exceptions=True)
        for client in (*(c for shards in self.clients.values() for c in shards), self.web):
            await client.aclose()

    # --- 模擬顧客 -----------------------------------------------------------

    def new_customer(self, body: dict) -> None:
        """一位新顧客：同時向每個目標下單，並各自追蹤到送達。"""
        for name in self.targets:
            task = asyncio.create_task(self._customer(name, body))
            self._customers.add(task)
            task.add_done_callback(self._customers.discard)

    async def _customer(self, name: str, body: dict) -> None:
        order = await self._request(name, "/api/orders", body, is_order=True)
        if order is None or order.outcome != "ok" or "tracking" not in (order.data or {}):
            return
        tracking = order.data["tracking"]
        failures = 0
        self.delivering[name] += 1
        try:
            while not self.stop.is_set():
                await self.sleep(self.track_interval_s)
                if self.stop.is_set():
                    return
                res = await self._request(name, "/api/track", {"order_id": body["order_id"], "tracking": tracking})
                if res is not None and res.outcome == "ok":
                    failures = 0
                    tracking = res.data.get("tracking", tracking)
                    if res.data.get("phase") == "delivered":
                        return
                    continue
                failures += 1
                if failures >= MAX_TRACK_FAILURES:
                    self.abandoned[name] += 1
                    return
        finally:
            self.delivering[name] -= 1

    async def _request(self, name: str, path: str, body: dict, is_order: bool = False) -> Result | None:
        """送出一個請求並計入統計；超過進行中上限時記為丟棄並回傳 None。"""
        if self.inflight[name] >= self.max_inflight:
            self.agg.dropped(name)
            return None
        self.agg.sent(name, order=is_order)
        self.inflight[name] += 1
        try:
            self._next_shard = (self._next_shard + 1) % POOL_SHARDS
            client = self.clients[name][self._next_shard]
            res = await send_request(client, self.targets[name], path, body, self.timeout_s)
        finally:
            self.inflight[name] -= 1
        self.agg.completed(name, res.outcome, res.latency_ms, res.instance_id)
        if self.on_complete:
            self.on_complete(name, res.outcome)
        return res

    async def sleep(self, seconds: float) -> None:
        """可被 stop 中斷的 sleep。"""
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def drain(self) -> None:
        """等待所有顧客的配送結束（送達或放棄），stop 時立即返回。"""
        while self._customers and not self.stop.is_set():
            await self.sleep(0.1)

    # --- 回報 ---------------------------------------------------------------

    async def _report_loop(self) -> None:
        next_t = self.start + 1
        while True:
            await asyncio.sleep(max(0.0, next_t - self.loop.time()))
            self._flush()
            next_t += 1

    def _flush(self) -> None:
        elapsed = self.loop.time() - self.start
        windows = self.agg.flush()
        for name, w in windows.items():
            total = self.totals[name]
            total.sent += w.sent
            total.orders += w.orders
            total.dropped += w.dropped
            for outcome, n in w.counts.items():
                total.counts[outcome] += n
        self.out(summary_line(elapsed, self.rate_fn(elapsed), windows, self.inflight, self.delivering))
        task = asyncio.ensure_future(self._post(report_payload(windows, self.rng)))
        self._reports.add(task)
        task.add_done_callback(self._reports.discard)

    async def _post(self, payload: dict) -> None:
        try:
            res = await self.web.post(f"{self.web_url}/api/reports/loadtest", json=payload)
            res.raise_for_status()
        except httpx.HTTPError as e:
            self.out(f"警告：回報 web 失敗（{type(e).__name__}: {e}），壓測繼續")

    def total_lines(self) -> list[str]:
        return [
            f"{name} 共下單 {w.orders} 請求 {w.sent} 成功 {w.counts['ok']} 逾時 {w.counts['timeout']} "
            f"忙碌 {w.counts['busy']} 錯誤 {w.counts['error']} 丟棄 {w.dropped} 放棄追蹤 {self.abandoned[name]}"
            for name, w in self.totals.items()
        ]


async def run_load(
    targets: dict[str, str],
    web_url: str,
    *,
    rate: float,
    ramp: float,
    duration: float,
    orders: OrderFactory,
    stop: asyncio.Event,
    out: Callable[[str], None] = print,
    max_inflight: int = MAX_INFLIGHT,
    timeout_s: float = REQUEST_TIMEOUT_S,
    track_interval_s: float = TRACK_INTERVAL_S,
    target_transport: httpx.AsyncBaseTransport | None = None,
    web_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """新顧客依到達速率出現，兩個目標相同速率、相同訂單內容；每位顧客追蹤到送達。"""
    session = Session(
        targets, web_url, stop=stop, out=out, max_inflight=max_inflight, timeout_s=timeout_s,
        track_interval_s=track_interval_s, target_transport=target_transport, web_transport=web_transport,
    )
    async with session as s:
        s.rate_fn = lambda elapsed: rate_at(elapsed, rate, ramp) if elapsed < duration else 0.0
        await run_schedule(rate, ramp, duration, lambda: s.new_customer(orders.next()), s.loop.time, s.sleep, s.stop)
        if not stop.is_set():
            out("排程結束，不再產生新顧客，等待進行中的配送完成…")
            await s.drain()
    for line in s.total_lines():
        out(line)


@dataclass
class Stage:
    rate: int
    completed: int = 0
    failed: int = 0

    @property
    def failure_rate(self) -> float:
        return self.failed / self.completed if self.completed else 0.0


async def run_probe(
    url: str,
    web_url: str,
    *,
    name: str = "fixed",
    step_s: float = 15,
    threshold: float = 0.05,
    max_rate: int = 50,
    orders: OrderFactory,
    stop: asyncio.Event,
    out: Callable[[str], None] = print,
    track_interval_s: float = TRACK_INTERVAL_S,
    target_transport: httpx.AsyncBaseTransport | None = None,
    web_transport: httpx.AsyncBaseTransport | None = None,
) -> int | None:
    """從 1 rps 起每 step_s 秒加 1 位新顧客/秒（含後續追蹤）。

    某階段期間完成的請求（下單與追蹤）失敗率超過 threshold 即停止，回傳前一階段的速率作為容量 C；
    被中斷時回傳 None。
    """
    current: list[Stage] = []

    def on_complete(target, outcome):
        stage = current[-1]
        stage.completed += 1
        if outcome != "ok":
            stage.failed += 1

    # probe 只輸出階段結果；每秒的明細不印，但回報失敗的警告照常顯示
    session = Session(
        {name: url}, web_url, stop=stop, out=lambda line: out(line) if line.startswith("警告") else None,
        track_interval_s=track_interval_s, target_transport=target_transport, web_transport=web_transport,
        on_complete=on_complete,
    )
    capacity = None
    async with session as s:
        for rate in range(1, max_rate + 1):
            stage = Stage(rate)
            current.append(stage)
            s.rate_fn = lambda elapsed, r=rate: r
            out(f"開始 {rate} rps（{step_s:g} 秒）")
            await run_schedule(rate, 0, step_s, lambda: s.new_customer(orders.next()), s.loop.time, s.sleep, s.stop)
            if stop.is_set():
                break
            out(f"階段 {rate:3d} rps：完成 {stage.completed} 失敗 {stage.failed}（{stage.failure_rate:.1%}）")
            if stage.failure_rate > threshold:
                capacity = rate - 1
                break
        else:
            capacity = max_rate

    if capacity is None:
        out("probe 已中斷，未得到容量")
    elif capacity >= max_rate:
        out(f"容量 C ≥ {max_rate} rps（達到 --max-rate 仍未超過失敗門檻）")
    else:
        out(f"容量 C = {capacity} rps")
    return capacity
