"""壓力測試執行：模擬顧客（下單後追蹤到送達）、每秒回報 web、probe 容量量測（README 第 7 節）。"""

import asyncio
import importlib.util
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from loadtest.orders import OrderFactory
from loadtest.schedule import rate_at, run_schedule
from loadtest.stats import Aggregator, Window, report_payload, summary_line, total_lines

MAX_INFLIGHT = 2000
REQUEST_TIMEOUT_S = 10.0
TIMEOUT_LATENCY_MS = 10000
REPORT_TIMEOUT_S = 3.0
TRACK_INTERVAL_S = 2.0
MAX_TRACK_FAILURES = 5  # 連續失敗這麼多次就放棄追蹤，像真實顧客關掉 App
# 下單失敗時像真實顧客一樣隔一下再按一次，最多試這麼多次。失敗的顧客若直接消失，
# 撐不住的一方會因為沒有後續的位置更新而突然變輕、短暫恢復，接著又被新顧客壓垮，反覆波動
MAX_ORDER_ATTEMPTS = 5
# httpcore 每個請求都會掃描整個連線池（O(連線數)），連線多時把池子拆小、輪流使用
POOL_SHARDS = 8
# 雲端（HTTPS）改用 HTTP/2，所有請求共用少數幾條連線；HTTP/1.1 每個進行中請求各佔一條連線，
# 上千條連線加上逾時後重連，會把家用路由器打掛。本機是 http://，httpx 照樣走 HTTP/1.1
HTTP2 = importlib.util.find_spec("h2") is not None
# 本機（http://）走 HTTP/1.1，每個進行中請求各佔一條連線。Windows 上 uvicorn 用 SelectorEventLoop，
# select() 最多 512 個 socket，超過時事件迴圈直接崩潰：程序還在、埠還在監聽，但再也不接受連線。
# 因此本機每個目標的連線數壓在這以下，多出的請求在壓測端排隊（計入延遲），如同 Cloud Run 前端的佇列。
LOCAL_MAX_CONNECTIONS = 256


def shard_connections(url: str, max_inflight: int, max_connections: int) -> int:
    """每個連線池（shard）的連線上限；只有本機目標受 max_connections 限制。"""
    n = -(-max_inflight // POOL_SHARDS)
    if not url.startswith("https://"):
        n = min(n, max_connections // POOL_SHARDS)
    return max(1, n)


_background: set[asyncio.Task] = set()  # 已逾時、仍在背景跑完的請求；保留參照避免被回收


def _finish_background(task: asyncio.Task) -> None:
    _background.discard(task)
    if not task.cancelled():
        task.exception()  # 取出例外，避免 asyncio 印出 "Task exception was never retrieved"


@dataclass
class Result:
    outcome: str
    latency_ms: float
    instance_id: str | None = None
    data: Any = None


async def send_request(
    client: httpx.AsyncClient, url: str, path: str, body: dict, timeout_s: float = REQUEST_TIMEOUT_S
) -> Result:
    """送出一個請求並依 README 分類，方式與手機端一致。

    逾時不取消請求：HTTP/2 的多個請求共用一條 TLS 連線，寫到一半被取消會讓整條連線的加密資料錯亂
    （SSLV3_ALERT_BAD_RECORD_MAC），連帶弄壞同一條連線上的其他請求。所以逾時先記為 timeout，
    請求留在背景跑完（由 httpx 自己的逾時或伺服器的逾時收尾）。
    """
    start = time.perf_counter()
    task = asyncio.ensure_future(client.post(f"{url}{path}", json=body))
    _background.add(task)
    task.add_done_callback(_finish_background)
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if not done:
        return Result("timeout", TIMEOUT_LATENCY_MS)
    try:
        res = task.result()
    except httpx.TimeoutException:
        return Result("timeout", TIMEOUT_LATENCY_MS)
    except (httpx.HTTPError, OSError):  # OSError 包含 httpx 未包裝的 ssl.SSLError
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
        max_connections: int = LOCAL_MAX_CONNECTIONS,
        timeout_s: float = REQUEST_TIMEOUT_S,
        track_interval_s: float = TRACK_INTERVAL_S,
        order_attempts: int = MAX_ORDER_ATTEMPTS,
        target_transport: httpx.AsyncBaseTransport | None = None,
        web_transport: httpx.AsyncBaseTransport | None = None,
        on_complete: Callable[[str, str], None] | None = None,
        on_window: Callable[[float, float, dict, dict, dict], None] | None = None,
    ):
        self.targets = {name: url.rstrip("/") for name, url in targets.items()}
        self.web_url = web_url.rstrip("/")
        self.stop = stop
        self.out = out
        self.max_inflight = max_inflight
        self.max_connections = max_connections
        self.timeout_s = timeout_s
        self.track_interval_s = track_interval_s
        self.order_attempts = order_attempts
        self.on_complete = on_complete
        self.on_window = on_window  # 有設定時每秒統計交給它，不印 summary_line
        self.rate_fn: Callable[[float], float] = lambda elapsed: 0.0
        self.agg = Aggregator(list(self.targets))
        self.inflight = dict.fromkeys(self.targets, 0)
        self.delivering = dict.fromkeys(self.targets, 0)
        self.abandoned = dict.fromkeys(self.targets, 0)
        self.gave_up = dict.fromkeys(self.targets, 0)  # 下單一直失敗而放棄的顧客
        self.totals = {name: Window() for name in self.targets}
        self._customers: set[asyncio.Task] = set()
        self._reports: set[asyncio.Task] = set()
        self._transports = (target_transport, web_transport)

    async def __aenter__(self) -> "Session":
        target_transport, web_transport = self._transports
        def limits(url: str) -> httpx.Limits:
            n = shard_connections(url, self.max_inflight, self.max_connections)
            return httpx.Limits(max_connections=n, max_keepalive_connections=n)

        self.clients = {
            name: [
                httpx.AsyncClient(limits=limits(url), timeout=self.timeout_s + 5, transport=target_transport, http2=HTTP2)
                for _ in range(POOL_SHARDS)
            ]
            for name, url in self.targets.items()
        }
        self._next_shard = 0
        self.web = httpx.AsyncClient(timeout=REPORT_TIMEOUT_S, transport=web_transport, http2=HTTP2)
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
        order = await self._order(name, body)
        if order is None or "tracking" not in (order.data or {}):
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

    async def _order(self, name: str, body: dict) -> Result | None:
        """下單，失敗時隔 track_interval_s 再按一次，最多 order_attempts 次；全部失敗回傳 None。"""
        for attempt in range(self.order_attempts):
            if attempt:
                await self.sleep(self.track_interval_s)
                if self.stop.is_set():
                    return None
            res = await self._request(name, "/api/orders", body, is_order=True)
            if res is not None and res.outcome == "ok":
                return res
        self.gave_up[name] += 1
        return None

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
        if self.on_window:
            self.on_window(elapsed, self.rate_fn(elapsed), windows, dict(self.inflight), dict(self.delivering))
        else:
            self.out(summary_line(elapsed, self.rate_fn(elapsed), windows, self.inflight, self.delivering))
        task = asyncio.ensure_future(self._post(report_payload(windows)))
        self._reports.add(task)
        task.add_done_callback(self._reports.discard)

    async def _post(self, payload: dict) -> None:
        try:
            res = await self.web.post(f"{self.web_url}/api/reports/loadtest", json=payload)
            res.raise_for_status()
        except httpx.HTTPError as e:
            self.out(f"警告：回報 web 失敗（{type(e).__name__}: {e}），壓測繼續")

    def total_lines(self) -> list[str]:
        return total_lines(self.totals, self.abandoned, self.gave_up)


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
    max_connections: int = LOCAL_MAX_CONNECTIONS,
    timeout_s: float = REQUEST_TIMEOUT_S,
    track_interval_s: float = TRACK_INTERVAL_S,
    target_transport: httpx.AsyncBaseTransport | None = None,
    web_transport: httpx.AsyncBaseTransport | None = None,
    on_window: Callable[[float, float, dict, dict, dict], None] | None = None,
    print_totals: bool = True,
) -> tuple[dict[str, Window], dict[str, int], dict[str, int]]:
    """新顧客依到達速率出現，兩個目標相同速率、相同訂單內容；每位顧客追蹤到送達。

    回傳各目標的累計統計、放棄追蹤數與放棄下單數。
    """
    session = Session(
        targets, web_url, stop=stop, out=out, max_inflight=max_inflight, max_connections=max_connections,
        timeout_s=timeout_s,
        track_interval_s=track_interval_s, target_transport=target_transport, web_transport=web_transport,
        on_window=on_window,
    )
    async with session as s:
        s.rate_fn = lambda elapsed: rate_at(elapsed, rate, ramp) if elapsed < duration else 0.0
        await run_schedule(rate, ramp, duration, lambda: s.new_customer(orders.next()), s.loop.time, s.sleep, s.stop)
        if not stop.is_set():
            out("排程結束，不再產生新顧客，等待進行中的配送完成…")
            await s.drain()
    if print_totals:
        for line in s.total_lines():
            out(line)
    return s.totals, s.abandoned, s.gave_up


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
        order_attempts=1,  # 量測容量時不重試，失敗率才反映該階段的速率
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
