"""壓力測試執行：開放式負載、每秒回報 web、probe 容量量測（README 第 7 節）。"""

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from loadtest.orders import OrderFactory
from loadtest.schedule import rate_at, run_schedule
from loadtest.stats import Aggregator, Window, report_payload, summary_line

MAX_INFLIGHT = 2000
REQUEST_TIMEOUT_S = 15.0
TIMEOUT_LATENCY_MS = 15000
REPORT_TIMEOUT_S = 3.0


async def send_order(client: httpx.AsyncClient, url: str, body: dict, timeout_s: float = REQUEST_TIMEOUT_S):
    """回傳 (outcome, latency_ms, instance_id)，分類方式與手機端一致。"""
    start = time.perf_counter()
    try:
        res = await asyncio.wait_for(client.post(f"{url}/api/orders", json=body), timeout_s)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return "timeout", TIMEOUT_LATENCY_MS, None
    except httpx.HTTPError:
        return "error", (time.perf_counter() - start) * 1000, None
    latency = (time.perf_counter() - start) * 1000
    if res.status_code == 200:
        try:
            instance_id = res.json().get("instance_id")
        except ValueError:
            instance_id = None
        return "ok", latency, instance_id
    return ("busy" if res.status_code == 429 else "error"), latency, None


class Session:
    """管理各目標的連線、進行中請求、每秒彙總與回報。"""

    def __init__(
        self,
        targets: dict[str, str],
        web_url: str,
        *,
        stop: asyncio.Event,
        out: Callable[[str], None],
        max_inflight: int = MAX_INFLIGHT,
        timeout_s: float = REQUEST_TIMEOUT_S,
        target_transport: httpx.AsyncBaseTransport | None = None,
        web_transport: httpx.AsyncBaseTransport | None = None,
        on_complete: Callable[[str, object, str], None] | None = None,
    ):
        self.targets = {name: url.rstrip("/") for name, url in targets.items()}
        self.web_url = web_url.rstrip("/")
        self.stop = stop
        self.out = out
        self.max_inflight = max_inflight
        self.timeout_s = timeout_s
        self.on_complete = on_complete
        self.rate_fn: Callable[[float], float] = lambda elapsed: 0.0
        self.agg = Aggregator(list(self.targets))
        self.inflight = dict.fromkeys(self.targets, 0)
        self.totals = {name: Window() for name in self.targets}
        self.rng = random.Random()
        self._tasks: set[asyncio.Task] = set()
        self._reports: set[asyncio.Task] = set()
        self._transports = (target_transport, web_transport)

    async def __aenter__(self) -> "Session":
        target_transport, web_transport = self._transports
        limits = httpx.Limits(max_connections=self.max_inflight, max_keepalive_connections=self.max_inflight)
        self.clients = {
            name: httpx.AsyncClient(limits=limits, timeout=self.timeout_s + 5, transport=target_transport)
            for name in self.targets
        }
        self.web = httpx.AsyncClient(timeout=REPORT_TIMEOUT_S, transport=web_transport)
        self.loop = asyncio.get_running_loop()
        self.start = self.loop.time()
        self._reporter = asyncio.create_task(self._report_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        self._reporter.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(self._reporter, *self._tasks, return_exceptions=True)
        self._flush()  # 最後一筆回報
        await asyncio.gather(*self._reports, return_exceptions=True)
        for client in (*self.clients.values(), self.web):
            await client.aclose()

    # --- 發送 ---------------------------------------------------------------

    def fire(self, body: dict, tag: object = None) -> None:
        for name in self.targets:
            if self.inflight[name] >= self.max_inflight:
                self.agg.dropped(name)
                continue
            self.agg.sent(name)
            self.inflight[name] += 1
            task = asyncio.create_task(self._send(name, body, tag))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _send(self, name: str, body: dict, tag: object) -> None:
        try:
            outcome, latency, instance_id = await send_order(self.clients[name], self.targets[name], body, self.timeout_s)
        finally:
            self.inflight[name] -= 1
        self.agg.completed(name, outcome, latency, instance_id)
        if self.on_complete:
            self.on_complete(name, tag, outcome)

    async def sleep(self, seconds: float) -> None:
        """可被 stop 中斷的 sleep。"""
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def drain(self) -> None:
        """等待進行中的請求完成（最多 timeout_s），stop 時立即返回。"""
        while self._tasks and not self.stop.is_set():
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
            total.dropped += w.dropped
            for outcome, n in w.counts.items():
                total.counts[outcome] += n
        self.out(summary_line(elapsed, self.rate_fn(elapsed), windows, self.inflight))
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
            f"{name} 共送出 {w.sent} 成功 {w.counts['ok']} 逾時 {w.counts['timeout']} "
            f"忙碌 {w.counts['busy']} 錯誤 {w.counts['error']} 丟棄 {w.dropped}"
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
    target_transport: httpx.AsyncBaseTransport | None = None,
    web_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """兩個目標以相同速率、相同訂單內容同時施壓。"""
    session = Session(
        targets, web_url, stop=stop, out=out, max_inflight=max_inflight, timeout_s=timeout_s,
        target_transport=target_transport, web_transport=web_transport,
    )
    async with session as s:
        s.rate_fn = lambda elapsed: rate_at(elapsed, rate, ramp) if elapsed < duration else 0.0
        await run_schedule(rate, ramp, duration, lambda: s.fire(orders.next()), s.loop.time, s.sleep, s.stop)
        if not stop.is_set():
            out("排程結束，等待進行中的請求完成…")
            await s.drain()
    for line in s.total_lines():
        out(line)


@dataclass
class Stage:
    rate: int
    sent: int = 0
    done: int = 0
    failed: int = 0
    finished_sending: bool = False

    @property
    def complete(self) -> bool:
        return self.finished_sending and self.done >= self.sent

    @property
    def failure_rate(self) -> float:
        return self.failed / self.sent if self.sent else 0.0


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
    target_transport: httpx.AsyncBaseTransport | None = None,
    web_transport: httpx.AsyncBaseTransport | None = None,
) -> int | None:
    """從 1 rps 起每 step_s 秒加 1 rps；某階段送出的請求失敗率超過 threshold 即停止。

    回傳前一階段的速率作為容量 C；被中斷時回傳 None。
    """
    stages: dict[int, Stage] = {}
    reported: set[int] = set()

    def on_complete(target, tag, outcome):
        stage = stages[tag]
        stage.done += 1
        if outcome != "ok":
            stage.failed += 1

    def evaluate() -> int | None:
        """依序檢查已完成的階段，回傳第一個失敗的階段速率。"""
        for rate in sorted(stages):
            stage = stages[rate]
            if not stage.complete:
                return None
            if rate not in reported:
                reported.add(rate)
                out(f"階段 {rate:3d} rps：送出 {stage.sent} 失敗 {stage.failed}（{stage.failure_rate:.1%}）")
            if stage.failure_rate > threshold:
                return rate
        return None

    # probe 只輸出階段結果；每秒的明細不印，但回報失敗的警告照常顯示
    session = Session(
        {name: url}, web_url, stop=stop, out=lambda line: out(line) if line.startswith("警告") else None,
        target_transport=target_transport, web_transport=web_transport, on_complete=on_complete,
    )
    capacity = None
    async with session as s:
        for rate in range(1, max_rate + 1):
            stage = stages[rate] = Stage(rate)
            s.rate_fn = lambda elapsed, r=rate: r

            def fire(stage=stage):
                stage.sent += 1
                s.fire(orders.next(), tag=stage.rate)

            out(f"開始 {rate} rps（{step_s:g} 秒）")
            await run_schedule(rate, 0, step_s, fire, s.loop.time, s.sleep, s.stop)
            stage.finished_sending = True
            failed = evaluate()
            if failed is not None:
                capacity = failed - 1
                break
            if stop.is_set():
                break
        else:
            while not stop.is_set() and not all(st.complete for st in stages.values()):
                await s.sleep(0.2)
            failed = evaluate()
            capacity = max_rate if failed is None else failed - 1

    if capacity is None:
        out("probe 已中斷，未得到容量")
    elif capacity >= max_rate:
        out(f"容量 C ≥ {max_rate} rps（達到 --max-rate 仍未超過失敗門檻）")
    else:
        out(f"容量 C = {capacity} rps")
    return capacity
