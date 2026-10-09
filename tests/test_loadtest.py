import asyncio
import json
import random

import httpx
import pytest

from loadtest.orders import OrderFactory
from loadtest.runner import run_load, run_probe, send_order
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
    return httpx.Response(200, json={"instance_id": "i-1"})


@pytest.mark.parametrize(
    "handler, expected",
    [
        (_ok, ("ok", "i-1")),
        (lambda r: httpx.Response(429), ("busy", None)),
        (lambda r: httpx.Response(500), ("error", None)),
        (lambda r: httpx.Response(422), ("error", None)),
    ],
)
def test_send_order_outcomes(handler, expected):
    async def go():
        async with _client(handler) as c:
            return await send_order(c, "http://t", {}, timeout_s=1)

    outcome, latency, instance = asyncio.run(go())
    assert (outcome, instance) == expected
    assert latency >= 0


def test_send_order_network_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    async def go():
        async with _client(handler) as c:
            return await send_order(c, "http://t", {}, timeout_s=1)

    assert asyncio.run(go())[0] == "error"


def test_send_order_timeout_counts_15000():
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    async def go():
        async with _client(slow) as c:
            return await send_order(c, "http://t", {}, timeout_s=0.05)

    assert asyncio.run(go()) == ("timeout", 15000, None)


# --- 整合：Session / run_load / probe（MockTransport）-----------------------


class Hub:
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


def _run_load(hub, target_handler, *, duration, rate=20, stop_after=None, max_inflight=2000):
    lines = []

    async def go():
        stop = asyncio.Event()
        if stop_after is not None:
            asyncio.get_running_loop().call_later(stop_after, stop.set)
        await run_load(
            {"fixed": "http://fixed", "auto": "http://auto"}, "http://hub",
            rate=rate, ramp=0, duration=duration, orders=OrderFactory(MAP, random.Random(0)), stop=stop,
            out=lines.append, target_transport=httpx.MockTransport(target_handler),
            hub_transport=httpx.MockTransport(hub.handler), max_inflight=max_inflight,
        )

    asyncio.run(go())
    return lines


def test_run_load_reports_every_second():
    hub = Hub()
    lines = _run_load(hub, _ok, duration=2.5, rate=20)
    assert abs(hub.total("fixed", "ok") - 50) <= 2
    assert hub.total("auto", "ok") == hub.total("fixed", "ok")
    assert len(hub.reports) >= 3
    assert any("fixed" in line and "auto" in line for line in lines)


def test_stop_sends_final_report_and_returns():
    hub = Hub()

    async def slowish(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"instance_id": "x"})

    lines = _run_load(hub, slowish, duration=30, rate=20, stop_after=1.3)
    sent = sum(int(line.split("送出 ")[1].split()[0]) for line in lines if "fixed 送出" in line)
    assert 20 <= hub.total("fixed", "ok") <= sent
    assert hub.reports, "final report missing"


def test_hub_report_failure_only_warns():
    hub = Hub(fail=True)
    lines = _run_load(hub, _ok, duration=1.5, rate=10)
    assert any("警告" in line for line in lines)
    assert any("fixed 送出" in line for line in lines)


def test_dropped_when_inflight_limit_reached():
    hub = Hub()

    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    lines = _run_load(hub, hang, duration=1.2, rate=20, stop_after=1.3, max_inflight=3)
    dropped = sum(int(line.split("丟棄 ")[1].split()[0]) for line in lines if "fixed 送出" in line)
    assert dropped >= 15


def test_probe_finds_capacity():
    """模擬容量約 3.5 rps 的服務：最近 2 秒超過 7 筆就回 429。"""
    hub = Hub()
    lines = []
    recent = []

    def limited(request):
        now = asyncio.get_running_loop().time()
        recent.append(now)
        while recent[0] <= now - 2.0:
            recent.pop(0)
        return httpx.Response(429 if len(recent) > 7 else 200, json={"instance_id": "p"})

    async def go():
        return await run_probe(
            "http://fixed", "http://hub", name="fixed", step_s=1.5, threshold=0.05, max_rate=10,
            orders=OrderFactory(MAP, random.Random(0), seed=7), stop=asyncio.Event(), out=lines.append,
            target_transport=httpx.MockTransport(limited), hub_transport=httpx.MockTransport(hub.handler),
        )

    capacity = asyncio.run(go())
    assert capacity == 3, lines
    assert any("容量" in line for line in lines)
    assert hub.total("fixed", "ok") > 0
