import asyncio
import json
import random

import httpx
import pytest

from loadtest.orders import OrderFactory
from loadtest.runner import MAX_TRACK_FAILURES, run_load, run_probe, send_request
from loadtest.schedule import rate_at, run_schedule, send_times
from loadtest.stats import Aggregator, report_payload
from shared.citymap import build_city

MAP = build_city(20).to_map_json()


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.t += max(0.0, d)


def _schedule(rate, ramp, duration):
    clock = FakeClock()
    start = clock.t
    fired = []
    asyncio.run(run_schedule(rate, ramp, duration, lambda: fired.append(clock.t - start), clock, clock.sleep))
    return fired


# --- 排程器 -----------------------------------------------------------------


def test_constant_rate_schedules_rate_times_duration():
    fired = _schedule(rate=10, ramp=0, duration=10)
    assert abs(len(fired) - 100) <= 1
    assert all(0 <= t < 10 for t in fired)


def test_ramp_rate_increases_linearly():
    fired = _schedule(rate=100, ramp=10, duration=10)
    per_second = [sum(1 for t in fired if i <= t < i + 1) for i in range(10)]
    for i, n in enumerate(per_second):
        assert abs(n - (10 * i + 5)) <= 1, per_second
    assert abs(len(fired) - 500) <= 1


def test_ramp_then_steady():
    fired = _schedule(rate=20, ramp=5, duration=10)
    steady = [sum(1 for t in fired if i <= t < i + 1) for i in range(5, 10)]
    assert all(abs(n - 20) <= 1 for n in steady)


def test_schedule_lasts_full_duration():
    """最後一筆送出後仍要等到 duration 結束，否則 probe 的下一階段會提早開始。"""
    clock = FakeClock()
    start = clock.t
    asyncio.run(run_schedule(1, 0, 1.5, lambda: None, clock, clock.sleep))
    assert clock.t - start == pytest.approx(1.5)


def test_rate_at():
    assert rate_at(0, 60, 60) == 0
    assert rate_at(30, 60, 60) == 30
    assert rate_at(90, 60, 60) == 60
    assert rate_at(0, 60, 0) == 60


def test_send_times_are_increasing():
    times = list(send_times(50, 7, 12))
    assert times == sorted(times)
    assert times[0] == 0


def test_schedule_stops_when_stop_set():
    clock = FakeClock()
    stop = asyncio.Event()
    fired = []

    def fire():
        fired.append(clock.t)
        if len(fired) == 5:
            stop.set()

    asyncio.run(run_schedule(10, 0, 10, fire, clock, clock.sleep, stop=stop))
    assert len(fired) == 5


# --- 彙總 -------------------------------------------------------------------


def test_aggregate_counts_and_samples_capped():
    agg = Aggregator(["fixed", "auto"])
    for i in range(500):
        agg.sent("fixed")
        agg.completed("fixed", "ok", float(i), "inst-a" if i % 2 else "inst-b")
    for _ in range(7):
        agg.completed("fixed", "timeout", 15000, None)
    agg.completed("fixed", "busy", 30, None)
    agg.completed("fixed", "error", 5, None)
    agg.dropped("auto")
    windows = agg.flush()
    payload = report_payload(windows, random.Random(0))
    fixed = payload["targets"]["fixed"]
    assert (fixed["ok"], fixed["timeout"], fixed["busy"], fixed["error"]) == (500, 7, 1, 1)
    assert len(fixed["latency_samples_ms"]) == 200
    assert sorted(fixed["instance_ids"]) == ["inst-a", "inst-b"]
    assert payload["targets"]["auto"] == {
        "ok": 0, "timeout": 0, "busy": 0, "error": 0, "latency_samples_ms": [], "instance_ids": [],
    }
    assert windows["fixed"].sent == 500 and windows["auto"].dropped == 1
    assert "dropped" not in json.dumps(payload)


def test_aggregate_keeps_all_samples_under_cap():
    agg = Aggregator(["auto"])
    for v in (5, 6, 7):
        agg.completed("auto", "ok", v, None)
    payload = report_payload(agg.flush(), random.Random(0))
    assert sorted(payload["targets"]["auto"]["latency_samples_ms"]) == [5, 6, 7]


def test_flush_resets_window():
    agg = Aggregator(["auto"])
    agg.completed("auto", "ok", 5, None)
    agg.flush()
    assert report_payload(agg.flush(), random.Random(0))["targets"]["auto"]["ok"] == 0


# --- 訂單 -------------------------------------------------------------------


def test_orders_use_map_menu():
    factory = OrderFactory(MAP, random.Random(1))
    menus = {r["id"]: {m["id"] for m in r["menu"]} for r in MAP["restaurants"]}
    bodies = [factory.next() for _ in range(50)]
    assert len({b["order_id"] for b in bodies}) == 50
    assert {bool(b["customer"]["address"]) for b in bodies} == {True, False}
    for b in bodies:
        assert b["items"] and all(i["item_id"] in menus[b["restaurant_id"]] for i in b["items"])
        assert all(i["qty"] >= 1 for i in b["items"])
        assert b["seed"] is None


def test_orders_with_fixed_seed():
    a = OrderFactory(MAP, random.Random(1), seed=42)
    b = OrderFactory(MAP, random.Random(1), seed=42)
    assert [a.next() for _ in range(5)] == [b.next() for _ in range(5)]
    assert a.next()["seed"] == 42


# --- 單筆請求分類 -----------------------------------------------------------


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _ok(request):
    return httpx.Response(200, json={"instance_id": "i-1", "x": 1})


@pytest.mark.parametrize(
    "handler, expected",
    [
        (_ok, ("ok", "i-1")),
        (lambda r: httpx.Response(429), ("busy", None)),
        (lambda r: httpx.Response(500), ("error", None)),
        (lambda r: httpx.Response(422), ("error", None)),
    ],
)
def test_send_request_outcomes(handler, expected):
    async def go():
        async with _client(handler) as c:
            return await send_request(c, "http://t", "/api/orders", {}, timeout_s=1)

    r = asyncio.run(go())
    assert (r.outcome, r.instance_id) == expected
    assert r.latency_ms >= 0
    if r.outcome == "ok":
        assert r.data == {"instance_id": "i-1", "x": 1}


def test_send_request_network_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    async def go():
        async with _client(handler) as c:
            return await send_request(c, "http://t", "/api/track", {}, timeout_s=1)

    assert asyncio.run(go()).outcome == "error"


def test_send_request_timeout_counts_15000():
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    async def go():
        async with _client(slow) as c:
            return await send_request(c, "http://t", "/api/orders", {}, timeout_s=0.05)

    r = asyncio.run(go())
    assert (r.outcome, r.latency_ms, r.instance_id) == ("timeout", 15000, None)


# --- 整合：模擬顧客（MockTransport）----------------------------------------


class Web:
    def __init__(self, fail=False):
        self.reports = []
        self.fail = fail

    def handler(self, request):
        if self.fail:
            return httpx.Response(503)
        self.reports.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    def total(self, target, outcome):
        return sum(r["targets"].get(target, {}).get(outcome, 0) for r in self.reports)


class FakeDispatch:
    """下單回傳初始 tracking；追蹤 track_steps 次後送達。"""

    def __init__(self, track_steps=2, order_status=200, track_status=200, order_delay=0.0):
        self.track_steps = track_steps
        self.order_status = order_status
        self.track_status = track_status
        self.order_delay = order_delay
        self.orders = 0
        self.tracks = 0
        self.inflight = {}
        self.max_concurrent = 0

    async def handler(self, request):
        body = json.loads(request.content)
        if request.url.path == "/api/orders":
            self.orders += 1
            if self.order_delay:
                await asyncio.sleep(self.order_delay)
            if self.order_status != 200:
                return httpx.Response(self.order_status)
            return httpx.Response(200, json={"instance_id": "i-1", "tracking": {"step": 0}})
        key = (request.url.host, body["order_id"])
        self.inflight[key] = self.inflight.get(key, 0) + 1
        self.max_concurrent = max(self.max_concurrent, self.inflight[key])
        try:
            await asyncio.sleep(0.005)
            self.tracks += 1
            if self.track_status != 200:
                return httpx.Response(self.track_status)
            step = body["tracking"]["step"] + 1
            phase = "delivered" if step >= self.track_steps else "to_customer"
            return httpx.Response(200, json={"instance_id": "i-1", "phase": phase, "tracking": {"step": step}})
        finally:
            self.inflight[key] -= 1


def _run_load(web, dispatch, *, duration, rate=20, stop_after=None, max_inflight=2000, track_interval=0.05):
    lines = []

    async def go():
        stop = asyncio.Event()
        if stop_after is not None:
            asyncio.get_running_loop().call_later(stop_after, stop.set)
        await run_load(
            {"fixed": "http://fixed", "auto": "http://auto"}, "http://web",
            rate=rate, ramp=0, duration=duration, orders=OrderFactory(MAP, random.Random(0)), stop=stop,
            out=lines.append, target_transport=httpx.MockTransport(dispatch.handler),
            web_transport=httpx.MockTransport(web.handler), max_inflight=max_inflight,
            track_interval_s=track_interval,
        )

    asyncio.run(go())
    return lines


def _field(line, label):
    """從終端機輸出中取出 fixed 那一段的某個數字欄位。"""
    fixed_part = line.split("fixed ", 1)[1].split(" | ")[0]
    return int(fixed_part.split(f"{label} ")[1].split()[0])


def _sum(lines, label):
    return sum(_field(line, label) for line in lines if "fixed 下單" in line)


def test_customers_track_until_delivered():
    web, fake = Web(), FakeDispatch(track_steps=3)
    lines = _run_load(web, fake, duration=1.0, rate=10)
    orders = fake.orders // 2  # 兩個目標各一次
    assert abs(orders - 10) <= 1
    assert fake.tracks == fake.orders * 3
    assert fake.max_concurrent == 1, "同一位顧客同時只能有一個追蹤請求"
    # 回報給 web 的計數包含下單與追蹤
    assert web.total("fixed", "ok") == orders * 4
    assert any("配送中" in line for line in lines)


def test_waits_for_deliveries_after_duration():
    web, fake = Web(), FakeDispatch(track_steps=15)
    lines = _run_load(web, fake, duration=0.5, rate=10)
    assert fake.tracks == fake.orders * 15
    last = [line for line in lines if "fixed 下單" in line][-1]
    assert _field(last, "配送中") == 0
    assert any("等待進行中的配送" in line for line in lines)


def test_abandons_after_consecutive_track_failures():
    web, fake = Web(), FakeDispatch(track_status=500)
    lines = _run_load(web, fake, duration=0.5, rate=10)
    assert fake.tracks == fake.orders * MAX_TRACK_FAILURES
    assert any("放棄" in line for line in lines)


def test_failed_order_is_not_tracked():
    web, fake = Web(), FakeDispatch(order_status=429)
    _run_load(web, fake, duration=0.5, rate=10)
    assert fake.tracks == 0
    assert web.total("fixed", "busy") == fake.orders // 2


def test_run_load_reports_every_second():
    web, fake = Web(), FakeDispatch(track_steps=1)
    _run_load(web, fake, duration=2.5, rate=20)
    assert abs(web.total("fixed", "ok") - 100) <= 4  # 50 筆下單 + 50 筆追蹤
    assert web.total("auto", "ok") == web.total("fixed", "ok")
    assert len(web.reports) >= 3


def test_stop_sends_final_report_and_returns():
    web, fake = Web(), FakeDispatch(track_steps=10_000)
    lines = _run_load(web, fake, duration=30, rate=20, stop_after=1.3)
    assert 20 <= web.total("fixed", "ok") <= _sum(lines, "請求")
    assert web.reports, "final report missing"


def test_web_report_failure_only_warns():
    web, fake = Web(fail=True), FakeDispatch(track_steps=1)
    lines = _run_load(web, fake, duration=1.5, rate=10)
    assert any("警告" in line for line in lines)
    assert any("fixed 下單" in line for line in lines)


def test_dropped_when_inflight_limit_reached():
    web, fake = Web(), FakeDispatch(order_delay=5)
    lines = _run_load(web, fake, duration=1.2, rate=20, stop_after=1.3, max_inflight=3)
    assert _sum(lines, "丟棄") >= 15


def test_probe_finds_capacity():
    """模擬下單容量約 3.5 rps 的服務：最近 2 秒超過 7 筆下單就回 429；追蹤一次就送達。"""
    web = Web()
    lines = []
    recent = []

    def limited(request):
        if request.url.path == "/api/track":
            return httpx.Response(200, json={"instance_id": "p", "phase": "delivered", "tracking": {}})
        now = asyncio.get_running_loop().time()
        recent.append(now)
        while recent[0] <= now - 2.0:
            recent.pop(0)
        return httpx.Response(429 if len(recent) > 7 else 200, json={"instance_id": "p", "tracking": {}})

    async def go():
        return await run_probe(
            "http://fixed", "http://web", name="fixed", step_s=1.5, threshold=0.05, max_rate=10,
            orders=OrderFactory(MAP, random.Random(0), seed=7), stop=asyncio.Event(), out=lines.append,
            target_transport=httpx.MockTransport(limited), web_transport=httpx.MockTransport(web.handler),
            track_interval_s=0.05,
        )

    capacity = asyncio.run(go())
    assert capacity == 3, lines
    assert any("容量" in line for line in lines)
    assert web.total("fixed", "ok") > 0


# --- 壓測工具本身的效能（避免壓測工具成為瓶頸）------------------------------


def test_httpcore_async_detection_is_cached():
    """httpcore 每個請求都會 import sniffio；沒有安裝時每次都會掃描整個 sys.path，
    讓壓測工具在每秒數百個請求時吃滿 CPU。sniffio 必須存在，import 才會被快取。"""
    import sys

    from httpcore._synchronization import current_async_library

    async def detect():
        return current_async_library()

    assert asyncio.run(detect()) == "asyncio"
    assert sys.modules.get("sniffio") is not None


def test_requests_rotate_across_pool_shards(monkeypatch):
    """每個目標拆成多個小連線池輪流使用，避免 httpcore 每次掃描整個大連線池。"""
    from loadtest import runner

    used = []

    async def fake_send(client, url, path, body, timeout_s):
        used.append(id(client))
        return runner.Result("ok", 1.0, "i", {"tracking": {}, "phase": "delivered"})

    monkeypatch.setattr(runner, "send_request", fake_send)

    async def go():
        session = runner.Session({"fixed": "http://f"}, "http://web", stop=asyncio.Event(), out=lambda line: None,
                                 web_transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        async with session as s:
            assert len(s.clients["fixed"]) == runner.POOL_SHARDS
            for _ in range(runner.POOL_SHARDS * 2):
                await s._request("fixed", "/api/orders", {})

    asyncio.run(go())
    assert len(set(used)) == runner.POOL_SHARDS
