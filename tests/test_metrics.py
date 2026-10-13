import pytest

from web.metrics import Metrics, clean_nickname

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
    return Metrics(clock=clock)


def _lt(fixed=None, auto=None):
    empty = {"ok": 0, "timeout": 0, "busy": 0, "error": 0, "latency_sum_ms": 0, "latency_count": 0, "instance_ids": []}
    return {"fixed": {**empty, **(fixed or {})}, "auto": {**empty, **(auto or {})}}


def _student(fixed_outcome="ok", auto_outcome="ok", fixed_id="f1", auto_id="a1"):
    return [
        {"target": "fixed", "outcome": fixed_outcome, "latency_ms": 300, "instance_id": fixed_id},
        {"target": "auto", "outcome": auto_outcome, "latency_ms": 200, "instance_id": auto_id},
    ]


def _track(target="auto", outcome="ok", latency_ms=12, instance_id="a1"):
    return {"target": target, "outcome": outcome, "latency_ms": latency_ms, "instance_id": instance_id}


# --- 平均回應時間 ---------------------------------------------------------------


def test_avg_combines_loadtest_orders_and_tracks(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 4, "latency_sum_ms": 1000, "latency_count": 4}))
    metrics.record_order(_student())
    metrics.record_track(_track(target="fixed", latency_ms=200))
    fixed = metrics.snapshot(T0 + 1)["targets"]["fixed"]
    assert fixed["avg_ms"] == pytest.approx((1000 + 300 + 200) / 6)


def test_student_timeout_counted_as_10000ms(metrics, clock):
    metrics.record_order([{"target": "fixed", "outcome": "timeout", "latency_ms": 50, "instance_id": None}])
    metrics.record_track(_track(target="fixed", outcome="timeout", latency_ms=900, instance_id=None))
    assert metrics.snapshot(T0 + 1)["targets"]["fixed"]["avg_ms"] == 10000


def test_busy_and_error_not_in_latency(metrics, clock):
    """忙碌與錯誤很快就回來，算進延遲會讓服務越爛、延遲看起來越好。"""
    metrics.record_order([{"target": "fixed", "outcome": "busy", "latency_ms": 5, "instance_id": None}])
    metrics.record_track(_track(target="fixed", outcome="error", latency_ms=7, instance_id=None))
    metrics.record_track(_track(target="fixed", outcome="ok", latency_ms=300, instance_id="f1"))
    fixed = metrics.snapshot(T0 + 1)["targets"]["fixed"]
    assert fixed["avg_ms"] == 300
    assert fixed["success_rate"] == pytest.approx(1 / 3)
    assert fixed["failures"] == {"timeout": 0, "busy": 1, "error": 1}


def test_all_failed_has_requests_but_no_latency(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"error": 81}))
    fixed = metrics.snapshot(T0 + 1)["targets"]["fixed"]
    assert (fixed["avg_ms"], fixed["rps"], fixed["success_rate"]) == (None, 8.1, 0)


# --- 滾動視窗 ---------------------------------------------------------------


def test_window_is_last_10_completed_seconds(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 6, "timeout": 2, "latency_sum_ms": 20600, "latency_count": 8}))
    assert metrics.snapshot(T0)["targets"]["fixed"]["rps"] == 0  # 目前這一秒還在寫入

    in_window = metrics.snapshot(T0 + 10)["targets"]["fixed"]
    assert in_window["rps"] == pytest.approx(8 / 10)
    assert in_window["avg_ms"] == pytest.approx(20600 / 8)
    assert in_window["failures"] == {"timeout": 2, "busy": 0, "error": 0}

    expired = metrics.snapshot(T0 + 11)["targets"]["fixed"]
    assert expired["success_rate"] is None
    assert expired["avg_ms"] is None
    assert expired["rps"] == 0
    assert expired["failures"] == {"timeout": 0, "busy": 0, "error": 0}


def test_rps_combines_students_and_loadtest(metrics, clock):
    metrics.record_loadtest(_lt(auto={"ok": 18}))
    metrics.record_order(_student())
    metrics.record_track(_track())
    assert metrics.snapshot(T0 + 1)["targets"]["auto"]["rps"] == pytest.approx(2.0)


def test_bucketed_by_web_receive_time(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 1}))
    clock.t = T0 + 5
    metrics.record_loadtest(_lt(fixed={"ok": 1}))
    snap = metrics.snapshot(T0 + 12)["targets"]["fixed"]
    assert snap["rps"] == pytest.approx(1 / 10)


def test_ring_slot_reused_after_600s(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"error": 5}))
    clock.t = T0 + 600
    metrics.record_loadtest(_lt(fixed={"ok": 1, "latency_sum_ms": 9, "latency_count": 1}))
    snap = metrics.snapshot(clock.t + 1)["targets"]["fixed"]
    assert snap["failures"]["error"] == 0
    assert snap["success_rate"] == 1.0
    assert snap["avg_ms"] == 9


# --- 成功率 -----------------------------------------------------------------


def test_success_rate_over_window(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 30}))
    clock.t = T0 + 1
    metrics.record_loadtest(_lt(fixed={"ok": 1, "timeout": 3}))
    assert metrics.snapshot(T0 + 1)["targets"]["fixed"]["success_rate"] == 1.0  # 目前這一秒還沒結束
    assert metrics.snapshot(T0 + 2)["targets"]["fixed"]["success_rate"] == pytest.approx(31 / 34)
    assert metrics.snapshot(T0 + 11)["targets"]["fixed"]["success_rate"] == 0.25  # T0 已滑出視窗


# --- 活躍執行個體 -----------------------------------------------------------


def test_instance_expires_after_10s(metrics, clock):
    metrics.record_loadtest(_lt(auto={"ok": 2, "instance_ids": ["a", "b"]}))
    clock.t = T0 + 5
    metrics.record_order(_student(auto_id="b"))
    assert metrics.snapshot(T0 + 10)["targets"]["auto"]["instances"] == 2
    assert metrics.snapshot(T0 + 11)["targets"]["auto"]["instances"] == 1  # 只剩 T0+5 的 b
    assert metrics.snapshot(T0 + 16)["targets"]["auto"]["instances"] == 0


def test_null_instance_id_ignored(metrics, clock):
    metrics.record_order(_student(fixed_outcome="timeout", fixed_id=None))
    assert metrics.snapshot(T0 + 1)["targets"]["fixed"]["instances"] == 0


def test_student_tracks_keep_instance_active(metrics, clock):
    metrics.record_order(_student(fixed_id="f1", auto_id="a1"))
    for i in range(1, 30):
        clock.t = T0 + i
        metrics.record_track(_track())
    snap = metrics.snapshot(clock.t + 1)
    assert snap["targets"]["auto"]["instances"] == 1  # 下單 29 秒後仍在追蹤，仍算活躍
    assert snap["targets"]["auto"]["avg_ms"] == 12  # 追蹤也計入延遲
    assert snap["targets"]["fixed"]["instances"] == 0


# --- 趨勢 -------------------------------------------------------------------


def test_series_has_60_points_with_nulls(metrics, clock):
    snap = metrics.snapshot(clock.t)
    series = snap["series"]
    assert len(series) == 60
    assert [p["t"] for p in series] == list(range(series[0]["t"], series[0]["t"] + 600, 10))
    assert series[-1]["t"] <= clock.t < series[-1]["t"] + 10
    for p in series:
        assert p["fixed"] == {"success_rate": None, "avg_ms": None}
        assert p["auto"] == {"success_rate": None, "avg_ms": None}


def test_series_aggregates_10s_buckets(metrics, clock):
    clock.t = T0 - T0 % 10 - 100  # 某個 10 秒區間的開頭
    start = int(clock.t)
    metrics.record_loadtest(_lt(fixed={"ok": 1, "timeout": 1, "latency_sum_ms": 10100, "latency_count": 2}))
    clock.t += 9
    metrics.record_loadtest(_lt(fixed={"ok": 2, "latency_sum_ms": 500, "latency_count": 2}))
    series = metrics.snapshot(start + 100)["series"]
    point = next(p for p in series if p["t"] == start)
    assert point["fixed"]["success_rate"] == 0.75
    assert point["fixed"]["avg_ms"] == pytest.approx(10600 / 4)
    assert point["auto"] == {"success_rate": None, "avg_ms": None}
    later = next(p for p in series if p["t"] == start + 10)
    assert later["fixed"]["success_rate"] is None


def test_series_current_bucket_keeps_updating(metrics, clock):
    """快取已結束的區間後，目前區間的新資料仍要反映在趨勢中。"""
    for i in range(30):
        clock.t = T0 + i
        metrics.record_loadtest(_lt(fixed={"ok": 1, "latency_sum_ms": 100, "latency_count": 1}))
        series = metrics.snapshot(clock.t)["series"]
        assert sum(1 for p in series if p["fixed"]["success_rate"] == 1.0) == i // 10 + 1
    clock.t = T0 + 29
    metrics.record_loadtest(_lt(fixed={"error": 1}))
    last = metrics.snapshot(clock.t)["series"][-1]
    assert last["fixed"]["success_rate"] == pytest.approx(10 / 11)


def test_series_drops_data_older_than_600s(metrics, clock):
    metrics.record_loadtest(_lt(fixed={"ok": 1}))
    series = metrics.snapshot(T0 + 610)["series"]
    assert all(p["fixed"]["success_rate"] is None for p in series)


def test_snapshot_shape(metrics, clock):
    snap = metrics.snapshot(clock.t + 0.7)
    assert snap["now"] == T0
    assert set(snap) == {"now", "targets", "series"}
    assert set(snap["targets"]) == {"fixed", "auto"}
    assert set(snap["targets"]["fixed"]) == {"instances", "rps", "success_rate", "avg_ms", "failures", "machines", "requests"}


def test_snapshot_defaults_to_clock(metrics, clock):
    metrics.record_order(_student())
    clock.t = T0 + 1
    assert metrics.snapshot()["targets"]["auto"]["avg_ms"] == 200
    clock.t = T0 + 11
    assert metrics.snapshot()["targets"]["auto"]["avg_ms"] is None


# --- 機器格子 -------------------------------------------------------------------


def _client(cid, name="小明"):
    return {"id": cid, "name": name}


def _machines(metrics, now, target="auto"):
    return metrics.snapshot(now)["targets"][target]["machines"]


def test_clean_nickname():
    assert clean_nickname("  小明  ") == "小明"
    assert clean_nickname("") == "訪客"
    assert clean_nickname("   ") == "訪客"
    assert clean_nickname("a\u0000b​c") == "abc"  # 控制字元與零寬字元去掉
    assert clean_nickname("一二三四五六七八九十甲乙丙") == "一二三四五六七八九十甲乙"
    assert clean_nickname("<b>x</b>") == "<b>x</b>"  # 不改內容，顯示時一律當純文字


def test_machines_listed_in_first_seen_order(metrics, clock):
    metrics.record_loadtest(_lt(auto={"ok": 1, "instance_ids": ["inst-bbbb"], "instance_counts": {"inst-bbbb": 1}}))
    clock.t = T0 + 2
    metrics.record_loadtest(_lt(auto={"ok": 1, "instance_ids": ["inst-aaaa"], "instance_counts": {"inst-aaaa": 1}}))
    ms = _machines(metrics, T0 + 3)
    assert [m["id"] for m in ms] == ["bbbb", "aaaa"]
    assert {m["status"] for m in ms} == {"new"}


def test_machine_status_new_active_idle(metrics, clock):
    metrics.record_track(_track(instance_id="inst-0001"))
    clock.t = T0 + 29
    metrics.record_track(_track(instance_id="inst-0001"))
    assert _machines(metrics, T0 + 30)[0]["status"] == "new"  # 第一次出現在 30 秒內
    clock.t = T0 + 31
    metrics.record_track(_track(instance_id="inst-0001"))
    assert _machines(metrics, T0 + 32)[0]["status"] == "active"
    assert _machines(metrics, T0 + 50)[0]["status"] == "idle"  # 最近 10 秒沒接單
    assert _machines(metrics, T0 + 50)[0]["phone_count"] == 0
    assert _machines(metrics, T0 + 91)[0]["status"] == "idle"  # 最後一次接單在 T0+31，60 秒內
    assert _machines(metrics, T0 + 92) == []  # 60 秒內都沒接單就不列出


def test_phones_deduped_sorted_capped(metrics, clock):
    for i in range(25):
        metrics.record_track(_track(instance_id="inst-f001", target="fixed"), client=_client(f"c{i:02d}-xxxxxxxx", f"同學{i:02d}"))
    metrics.record_track(_track(instance_id="inst-f001", target="fixed"), client=_client("c00-xxxxxxxx", "同學00"))
    [m] = _machines(metrics, T0 + 1, "fixed")
    assert m["phone_count"] == 25
    assert len(m["phones"]) == 20
    assert m["phones"][0] == {"name": "同學00", "tag": "c00-xxxx"}
    assert [p["name"] for p in m["phones"]] == sorted(p["name"] for p in m["phones"])


def test_student_order_records_client_only_for_successful_targets(metrics, clock):
    metrics.record_order(_student(fixed_outcome="timeout", fixed_id=None, auto_id="inst-a001"), client=_client("abcdef1234", "  小美 "))
    assert _machines(metrics, T0 + 1, "fixed") == []
    [m] = _machines(metrics, T0 + 1, "auto")
    assert m["phones"] == [{"name": "小美", "tag": "abcdef12"}]


def test_report_without_client_still_lists_machine(metrics, clock):
    metrics.record_track(_track(instance_id="inst-a001"))
    [m] = _machines(metrics, T0 + 1)
    assert (m["phone_count"], m["phones"], m["loadtest_requests"]) == (0, [], 0)


def test_loadtest_counts_accumulate_and_old_format_is_null(metrics, clock):
    metrics.record_loadtest(_lt(auto={"ok": 3, "instance_ids": ["inst-a001"], "instance_counts": {"inst-a001": 3}}))
    clock.t = T0 + 1
    metrics.record_loadtest(_lt(auto={"ok": 2, "instance_ids": ["inst-a001"], "instance_counts": {"inst-a001": 2}}))
    metrics.record_loadtest(_lt(fixed={"ok": 1, "instance_ids": ["inst-f001"]}))  # 舊版壓測只送 instance_ids
    assert _machines(metrics, T0 + 2)[0]["loadtest_requests"] == 5
    assert _machines(metrics, T0 + 2, "fixed")[0]["loadtest_requests"] is None


def test_machine_ids_use_8_chars_when_last_4_collide(metrics, clock):
    metrics.record_loadtest(_lt(auto={"ok": 2, "instance_ids": ["aaaa0001", "bbbb0001"]}))
    assert [m["id"] for m in _machines(metrics, T0 + 1)] == ["aaaa0001", "bbbb0001"]


def test_first_seen_forgotten_after_600s(metrics, clock):
    metrics.record_track(_track(instance_id="inst-a001"))
    clock.t = T0 + 700
    metrics.snapshot(clock.t)  # 觸發清理
    metrics.record_track(_track(instance_id="inst-a001"))
    assert _machines(metrics, clock.t + 1)[0]["status"] == "new"


# --- 請求流向 -------------------------------------------------------------------


def test_requests_list_phone_requests_of_last_second(metrics, clock):
    metrics.record_track(_track(instance_id="inst-a001"), client=_client("aaaa1111-x", "小明"))
    metrics.record_order(_student(fixed_outcome="timeout", fixed_id=None, auto_id="inst-a002"), client=_client("bbbb2222-x", " 阿華 "))
    snap = metrics.snapshot(T0 + 1)
    assert snap["targets"]["auto"]["requests"] == [
        {"tag": "aaaa1111", "name": "小明", "machine": "a001", "outcome": "ok"},
        {"tag": "bbbb2222", "name": "阿華", "machine": "a002", "outcome": "ok"},
    ]
    assert snap["targets"]["fixed"]["requests"] == [{"tag": "bbbb2222", "name": "阿華", "machine": None, "outcome": "timeout"}]
    assert metrics.snapshot(T0 + 2)["targets"]["auto"]["requests"] == []  # 只推送最近 1 秒


def test_requests_without_client_not_listed(metrics, clock):
    metrics.record_track(_track(instance_id="inst-a001"))
    assert metrics.snapshot(T0 + 1)["targets"]["auto"]["requests"] == []


def test_requests_capped_at_200_per_side(metrics, clock):
    for i in range(250):
        metrics.record_track(_track(instance_id="inst-a001"), client=_client(f"c{i:04d}-xxxx", "同學"))
    assert len(metrics.snapshot(T0 + 1)["targets"]["auto"]["requests"]) == 200
