import random

import pytest

from web.metrics import Metrics, percentile

T0 = 1_760_000_000


class FakeClock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def clock():
    return FakeClock(T0)


@pytest.fixture()
def metrics(clock):
    return Metrics(clock=clock, rng=random.Random(0))


def _lt(fixed=None, auto=None):
    empty = {"ok": 0, "timeout": 0, "busy": 0, "error": 0, "latency_samples_ms": [], "instance_ids": []}
    return {"fixed": {**empty, **(fixed or {})}, "auto": {**empty, **(auto or {})}}


def _student(fixed_outcome="ok", auto_outcome="ok", fixed_id="f1", auto_id="a1"):
    return [
        {"target": "fixed", "outcome": fixed_outcome, "latency_ms": 300, "instance_id": fixed_id},
        {"target": "auto", "outcome": auto_outcome, "latency_ms": 200, "instance_id": auto_id},
    ]


# --- 分位數 -----------------------------------------------------------------


def test_percentile_known_samples():
    samples = list(range(1, 101))
    random.Random(1).shuffle(samples)
    assert percentile(samples, 50) == 50
    assert percentile(samples, 95) == 95
    assert percentile([7], 95) == 7
    assert percentile([], 50) is None


def test_snapshot_percentiles(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 100, "latency_samples_ms": list(range(1, 101))}))
    snap = metrics.snapshot(clock.t)
    assert snap["targets"]["fixed"]["p50_ms"] == 50
    assert snap["targets"]["fixed"]["p95_ms"] == 95


# --- 滾動視窗 ---------------------------------------------------------------


def test_data_older_than_60s_excluded(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 6, "timeout": 2, "latency_samples_ms": [100] * 8}))
    in_window = metrics.snapshot(T0 + 59)["targets"]["fixed"]
    assert in_window["rps"] == pytest.approx(8 / 60)
    assert in_window["failures"] == {"timeout": 2, "busy": 0, "error": 0}

    expired = metrics.snapshot(T0 + 60)["targets"]["fixed"]
    assert expired["success_rate"] is None
    assert expired["p50_ms"] is None and expired["p95_ms"] is None
    assert expired["rps"] == 0
    assert expired["failures"] == {"timeout": 0, "busy": 0, "error": 0}


def test_rps_combines_students_and_loadtest(metrics, clock):
    metrics.record_loadtest(_lt(auto={"ok": 119, "latency_samples_ms": [10]}))
    metrics.record_order(_student())
    assert metrics.snapshot(clock.t)["targets"]["auto"]["rps"] == pytest.approx(2.0)


def test_bucketed_by_web_receive_time(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 1, "latency_samples_ms": [1]}))
    clock.t = T0 + 30
    metrics.record_loadtest(_lt(fixed={"ok": 1, "latency_samples_ms": [1]}))
    snap = metrics.snapshot(T0 + 75)["targets"]["fixed"]
    assert snap["rps"] == pytest.approx(1 / 60)


def test_ring_slot_reused_after_600s(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"error": 5}))
    clock.t = T0 + 600
    metrics.record_loadtest(_lt(fixed={"ok": 1, "latency_samples_ms": [9]}))
    snap = metrics.snapshot(clock.t + 1)["targets"]["fixed"]
    assert snap["failures"]["error"] == 0
    assert snap["success_rate"] == 1.0


# --- 每秒成功率 -------------------------------------------------------------


def test_success_rate_uses_last_completed_second(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 30, "latency_samples_ms": [10]}))
    clock.t = T0 + 1
    metrics.record_loadtest(_lt(fixed={"ok": 1, "timeout": 3, "latency_samples_ms": [10]}))
    assert metrics.snapshot(T0 + 1)["targets"]["fixed"]["success_rate"] == 1.0  # 目前這一秒還沒結束
    assert metrics.snapshot(T0 + 2)["targets"]["fixed"]["success_rate"] == 0.25


def test_success_rate_looks_back_over_quiet_seconds(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 3, "error": 1, "latency_samples_ms": [10]}))
    assert metrics.snapshot(T0 + 10)["targets"]["fixed"]["success_rate"] == 0.75
    assert metrics.snapshot(T0 + 11)["targets"]["fixed"]["success_rate"] is None


# --- 活躍執行個體 -----------------------------------------------------------


def test_instance_expires_after_10s(metrics, clock):
    metrics.record_loadtest(_lt(auto={"ok": 2, "latency_samples_ms": [1, 2], "instance_ids": ["a", "b"]}))
    clock.t = T0 + 5
    metrics.record_order(_student(auto_id="b"))
    assert metrics.snapshot(T0 + 9)["targets"]["auto"]["instances"] == 2
    assert metrics.snapshot(T0 + 10)["targets"]["auto"]["instances"] == 1  # 只剩 T0+5 的 b
    assert metrics.snapshot(T0 + 15)["targets"]["auto"]["instances"] == 0


def test_null_instance_id_ignored(metrics, clock):
    metrics.record_order(_student(fixed_outcome="timeout", fixed_id=None))
    assert metrics.snapshot(clock.t)["targets"]["fixed"]["instances"] == 0


# --- 觀眾訂單 ---------------------------------------------------------------


def test_students_only_count_order_reports(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 50, "latency_samples_ms": [1]}, auto={"ok": 50, "latency_samples_ms": [1]}))
    assert metrics.snapshot(clock.t)["students"] == {"orders": 0, "fixed_ok": 0, "auto_ok": 0}

    metrics.record_order(_student(fixed_outcome="timeout"))
    metrics.record_order(_student())
    assert metrics.snapshot(clock.t)["students"] == {"orders": 2, "fixed_ok": 1, "auto_ok": 2}
    assert metrics.snapshot(T0 + 60)["students"] == {"orders": 0, "fixed_ok": 0, "auto_ok": 0}


def test_student_timeout_counted_as_10000ms(metrics, clock):
    metrics.record_order([{"target": "fixed", "outcome": "timeout", "latency_ms": 50, "instance_id": None}])
    assert metrics.snapshot(clock.t)["targets"]["fixed"]["p95_ms"] == 10000


# --- 樣本上限 ---------------------------------------------------------------


def test_latency_samples_capped_at_200_per_slot(metrics, clock):
    metrics.record_loadtest(_lt(auto={"ok": 150, "latency_samples_ms": [1] * 150}))
    metrics.record_loadtest(_lt(auto={"ok": 150, "latency_samples_ms": [2] * 150}))
    stored = metrics.samples_in_window("auto", clock.t, 60)
    assert len(stored) == 200
    assert {1, 2} <= set(stored)


# --- 趨勢 -------------------------------------------------------------------


def test_series_has_60_points_with_nulls(metrics, clock):
    snap = metrics.snapshot(clock.t)
    series = snap["series"]
    assert len(series) == 60
    assert [p["t"] for p in series] == list(range(series[0]["t"], series[0]["t"] + 600, 10))
    assert series[-1]["t"] <= clock.t < series[-1]["t"] + 10
    for p in series:
        assert p["fixed"] == {"success_rate": None, "p95_ms": None}
        assert p["auto"] == {"success_rate": None, "p95_ms": None}


def test_series_aggregates_10s_buckets(metrics, clock):
    clock.t = T0 - T0 % 10 - 100  # 某個 10 秒區間的開頭
    start = int(clock.t)
    metrics.record_loadtest(_lt(fixed={"ok": 1, "timeout": 1, "latency_samples_ms": [100, 10000]}))
    clock.t += 9
    metrics.record_loadtest(_lt(fixed={"ok": 2, "latency_samples_ms": [200, 300]}))
    series = metrics.snapshot(start + 100)["series"]
    point = next(p for p in series if p["t"] == start)
    assert point["fixed"]["success_rate"] == 0.75
    assert point["fixed"]["p95_ms"] == 10000
    assert point["auto"] == {"success_rate": None, "p95_ms": None}
    later = next(p for p in series if p["t"] == start + 10)
    assert later["fixed"]["success_rate"] is None


def test_series_current_bucket_keeps_updating(metrics, clock):
    """快取已結束的區間後，目前區間的新資料仍要反映在趨勢中。"""
    for i in range(30):
        clock.t = T0 + i
        metrics.record_loadtest(_lt(fixed={"ok": 1, "latency_samples_ms": [100]}))
        series = metrics.snapshot(clock.t)["series"]
        assert sum(1 for p in series if p["fixed"]["success_rate"] == 1.0) == i // 10 + 1
    clock.t = T0 + 29
    metrics.record_loadtest(_lt(fixed={"error": 1}))
    last = metrics.snapshot(clock.t)["series"][-1]
    assert last["fixed"]["success_rate"] == pytest.approx(10 / 11)


def test_series_drops_data_older_than_600s(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 1, "latency_samples_ms": [1]}))
    series = metrics.snapshot(T0 + 610)["series"]
    assert all(p["fixed"]["success_rate"] is None for p in series)


def test_snapshot_shape(metrics, clock):
    snap = metrics.snapshot(clock.t + 0.7)
    assert snap["now"] == T0
    assert set(snap) == {"now", "targets", "students", "series"}
    assert set(snap["targets"]) == {"fixed", "auto"}
    assert set(snap["targets"]["fixed"]) == {"instances", "rps", "success_rate", "p50_ms", "p95_ms", "failures"}


def test_snapshot_defaults_to_clock(metrics, clock):
    metrics.record_order(_student())
    clock.t = T0 + 61
    assert metrics.snapshot()["students"]["orders"] == 0


# --- 觀眾追蹤 ---------------------------------------------------------------


def test_student_tracks_keep_instance_active_but_not_counted_as_orders(metrics, clock):
    metrics.record_order(_student(fixed_id="f1", auto_id="a1"))
    for i in range(1, 30):
        clock.t = T0 + i
        metrics.record_track({"target": "auto", "outcome": "ok", "latency_ms": 12, "instance_id": "a1"})
    snap = metrics.snapshot(clock.t)
    assert snap["targets"]["auto"]["instances"] == 1  # 下單 29 秒後仍在追蹤，仍算活躍
    assert snap["targets"]["fixed"]["instances"] == 0
    assert snap["students"] == {"orders": 1, "fixed_ok": 1, "auto_ok": 1}
    assert snap["targets"]["auto"]["rps"] == pytest.approx(30 / 60)


def test_student_track_failure_counted(metrics, clock):
    metrics.record_track({"target": "fixed", "outcome": "timeout", "latency_ms": 900, "instance_id": None})
    t = metrics.snapshot(clock.t)["targets"]["fixed"]
    assert t["failures"]["timeout"] == 1
    assert t["p95_ms"] == 10000
