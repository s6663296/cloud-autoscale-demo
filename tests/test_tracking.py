import json
import random

import pytest

from dispatch.config import Params
from dispatch.engine import Order, dispatch_order, weight_table
from dispatch.tracking import InvalidTracking, advance, start_tracking
from shared.citymap import build_city

T0 = 1_760_000_000.0
SCALE = 40
PARAMS = Params(riders=30, rider_radius=10, candidates=3, alt_routes=2, alt_penalty=1.5, reliability_weight=0.5)
PHASES = ["to_restaurant", "waiting", "to_customer", "delivered"]


@pytest.fixture(scope="module")
def city():
    return build_city(30)


def _order_result(city, seed=1, address=""):
    r = city.restaurants[0]
    order = Order(restaurant_id=r["id"], items=[(r["menu"][0]["id"], 1)], address=address)
    return dispatch_order(order, T0, random.Random(seed), city=city, params=PARAMS)


def _uniform(city):
    return lambda minute: list(city.base_s)


def _run(city, state, step_s, weights_fn=None, limit=10_000):
    """以固定的真實時間間隔推進，直到送達；回傳每次的 view。"""
    views = []
    now = T0
    for _ in range(limit):
        now += step_s
        state, view = advance(state, now, city=city, time_scale=SCALE, weights_fn=weights_fn)
        views.append(view)
        if view["phase"] == "delivered":
            return state, views
    raise AssertionError("not delivered")


# --- 階段與路徑 -------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_phases_move_forward_and_visit_restaurant_then_customer(city, seed):
    result = _order_result(city, seed)
    state = start_tracking(result, T0)
    state, views = _run(city, state, 1.0)
    order = [PHASES.index(v["phase"]) for v in views]
    assert order == sorted(order), "phase went backwards"
    assert views[-1]["phase"] == "delivered"
    trail = [tuple(p) for v in views for p in v["trail"]]
    restaurant, customer = tuple(result["restaurant"]["node"]), tuple(result["customer_node"])
    assert restaurant in trail and customer in trail
    assert trail.index(restaurant) <= len(trail) - 1 - trail[::-1].index(customer)
    assert tuple(views[-1]["position"]) == customer


def test_trail_moves_along_open_roads(city):
    state = start_tracking(_order_result(city, 3), T0)
    state, views = _run(city, state, 0.5)
    points = [tuple(v["trail"][0]) for v in views] + [tuple(views[-1]["position"])]
    for v in views:
        for (ax, ay), (bx, by) in zip(v["trail"], v["trail"][1:]):
            assert abs(ax - bx) + abs(ay - by) <= 1 + 1e-9
    assert len(points) > 5


def test_waits_at_restaurant_until_pickup(city):
    result = _order_result(city, 2)
    state = start_tracking(result, T0)
    restaurant = list(result["restaurant"]["node"])
    _, views = _run(city, state, 0.5)
    for v in views:
        if v["phase"] == "to_customer":
            assert v["sim_s"] >= result["schedule"]["pickup_s"]
        if v["phase"] == "waiting":
            assert v["position"] == restaurant


def test_rider_already_at_restaurant(city):
    result = _order_result(city, 1)
    result = {**result, "route": {**result["route"], "to_restaurant": [result["restaurant"]["node"]]}}
    state = start_tracking(result, T0)
    _, view = advance(state, T0 + 1, city=city, time_scale=SCALE)
    assert view["phase"] in ("waiting", "to_customer")


# --- 時間一致性 -------------------------------------------------------------


@pytest.mark.parametrize("seed", range(4))
def test_delivery_time_independent_of_call_interval(city, seed):
    result = _order_result(city, seed)
    fine, _ = _run(city, start_tracking(result, T0), 1.0)
    coarse, _ = _run(city, start_tracking(result, T0), 5.0)
    assert abs(fine["delivered_s"] - coarse["delivered_s"]) <= 0.05 * fine["delivered_s"]


def test_matches_dispatch_schedule_with_same_traffic(city):
    """派單與追蹤使用同一分鐘的路況時，送達時間與派單的預估一致。"""
    result = _order_result(city, 4)
    fixed_minute = lambda minute: weight_table(city, int(T0 // 60))
    state, _ = _run(city, start_tracking(result, T0), 2.0, weights_fn=fixed_minute)
    assert state["delivered_s"] == pytest.approx(result["schedule"]["deliver_s"], abs=1.0)


def test_time_never_goes_backwards(city):
    state = start_tracking(_order_result(city, 1), T0)
    state, first = advance(state, T0 + 10, city=city, time_scale=SCALE)
    state2, second = advance(state, T0 + 5, city=city, time_scale=SCALE)
    assert second["sim_s"] == first["sim_s"]
    assert second["position"] == first["position"]


def test_eta_decreases_to_zero_with_constant_traffic(city):
    state = start_tracking(_order_result(city, 5), T0)
    _, views = _run(city, state, 1.0, weights_fn=_uniform(city))
    etas = [v["eta_s"] for v in views]
    assert all(b <= a for a, b in zip(etas, etas[1:]))
    assert etas[-1] == 0 and etas[0] > 0


def test_eta_plus_elapsed_is_stable_with_constant_traffic(city):
    state = start_tracking(_order_result(city, 5), T0)
    _, views = _run(city, state, 1.0, weights_fn=_uniform(city))
    totals = [v["sim_s"] + v["eta_s"] for v in views if v["phase"] != "delivered"]
    assert max(totals) - min(totals) <= 2


# --- 改道 -------------------------------------------------------------------


def test_reroutes_when_traffic_changes_but_no_u_turn(city):
    result = _order_result(city, 3)
    state = start_tracking(result, T0)
    uniform = _uniform(city)
    # 推進到外送員走在路段中途，且前方還有夠長的路線可以改道
    now = T0
    for _ in range(20_000):
        now += 0.02
        state, view = advance(state, now, city=city, time_scale=SCALE, weights_fn=uniform)
        if state["next"] is not None and 0.1 < state["edge_frac"] < 0.9 and len(view["route"]) > 4:
            break
    else:
        pytest.fail("rider never found mid-edge with a long route ahead")
    planned = [tuple(p) for p in view["route"]]
    nxt = tuple(state["next"])
    blocked = set()
    # 讓原本規劃的下一段之後的路段都變得極慢
    for a, b in zip(planned[1:], planned[2:]):
        for nb, e in city.adjacency[a]:
            if nb == b:
                blocked.add(e)
    slow = lambda minute: [w * 50 if i in blocked else w for i, w in enumerate(city.base_s)]
    state2, view2 = advance(state, now + 0.01, city=city, time_scale=SCALE, weights_fn=slow)
    assert tuple(state2["next"] or state2["node"]) == nxt or tuple(state2["node"]) == nxt  # 不迴轉
    new_route = [tuple(p) for p in view2["route"]]
    if len(planned) > 3:
        assert new_route[2:] != planned[2:], "should take a different route"


# --- 狀態序列化與驗證 -------------------------------------------------------


def test_state_survives_json_round_trip(city):
    state = start_tracking(_order_result(city, 1), T0)
    for step in range(1, 8):
        a_state, a_view = advance(state, T0 + step * 3, city=city, time_scale=SCALE)
        b_state, b_view = advance(json.loads(json.dumps(state)), T0 + step * 3, city=city, time_scale=SCALE)
        assert a_view == b_view and a_state == b_state
        state = json.loads(json.dumps(a_state))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.pop("phase"),
        lambda s: s.update(v=99),
        lambda s: s.update(phase="flying"),
        lambda s: s.update(node=[-1, 0]),
        lambda s: s.update(node=[999, 0]),
        lambda s: s.update(node="a"),
        lambda s: s.update(next=[5, 5], node=[0, 0]),
        lambda s: s.update(edge_frac=1.5),
        lambda s: s.update(sim_s=-3),
        lambda s: s.update(dispatched_at="yesterday"),
        lambda s: s.update(customer=[0]),
    ],
)
def test_invalid_state_rejected(city, mutate):
    state = start_tracking(_order_result(city, 1), T0)
    mutate(state)
    with pytest.raises(InvalidTracking):
        advance(state, T0 + 1, city=city, time_scale=SCALE)


def test_rejects_non_dict(city):
    with pytest.raises(InvalidTracking):
        advance("nope", T0, city=city, time_scale=SCALE)
