import random

import pytest

from dispatch.config import Params
from dispatch.engine import (
    InvalidOrder,
    Order,
    dispatch_order,
    plan_order,
    traffic_factor,
    weight_table,
)
from dispatch.routing import alternative_routes, dijkstra_all, dijkstra_path
from shared.citymap import build_city

NOW = 1_760_000_000.0
PARAMS = Params(riders=30, rider_radius=15, candidates=5, alt_routes=3, alt_penalty=1.5, reliability_weight=0.5)


@pytest.fixture(scope="module")
def small():
    return build_city(5)


@pytest.fixture(scope="module")
def city():
    return build_city(30)


def _random_weights(city, seed):
    rng = random.Random(seed)
    return [rng.uniform(1, 10) for _ in city.edges]


def _brute_force(city, weights, source):
    """窮舉所有從 source 出發的簡單路徑，記錄到每個路口的最小成本。"""
    best = {source: 0.0}

    def dfs(node, cost, visited):
        for nb, e in city.adjacency[node]:
            if nb in visited:
                continue
            c = cost + weights[e]
            if c < best.get(nb, float("inf")):
                best[nb] = c
            visited.add(nb)
            dfs(nb, c, visited)
            visited.remove(nb)

    dfs(source, 0.0, {source})
    return best


def _assert_valid_route(city, weights, route, source, target):
    assert route.nodes[0] == source and route.nodes[-1] == target
    assert len(route.edges) == len(route.nodes) - 1
    for (u, v), e in zip(zip(route.nodes, route.nodes[1:]), route.edges):
        assert (v, e) in city.adjacency[u]
    assert route.cost == pytest.approx(sum(weights[e] for e in route.edges))


# --- 最短路徑 ---------------------------------------------------------------


@pytest.mark.parametrize("source", [(0, 0), (2, 2), (4, 1)])
def test_dijkstra_all_matches_brute_force(small, source):
    weights = _random_weights(small, seed=hash(source) & 0xFFFF)
    dist, _ = dijkstra_all(small.adjacency, weights, source)
    expected = _brute_force(small, weights, source)
    assert dist.keys() == expected.keys()
    for node, d in expected.items():
        assert dist[node] == pytest.approx(d)


def test_dijkstra_path_matches_full_search(small):
    weights = _random_weights(small, seed=7)
    dist, _ = dijkstra_all(small.adjacency, weights, (0, 0))
    for target in small.adjacency:
        route = dijkstra_path(small.adjacency, weights, (0, 0), target)
        assert route.cost == pytest.approx(dist[target])
        _assert_valid_route(small, weights, route, (0, 0), target)


def test_dijkstra_path_to_self_is_empty(small):
    route = dijkstra_path(small.adjacency, _random_weights(small, 1), (3, 3), (3, 3))
    assert route.nodes == [(3, 3)] and route.edges == [] and route.cost == 0


# --- 替代路線 ---------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 3, 5])
def test_alternative_routes_count_and_distinct(city, k):
    weights = weight_table(city, minute=100)
    routes = alternative_routes(city.adjacency, weights, (3, 4), (20, 25), k, 1.5)
    assert len(routes) == k
    assert len({tuple(r.edges) for r in routes}) == k
    for r in routes:
        _assert_valid_route(city, weights, r, (3, 4), (20, 25))


def test_first_alternative_is_shortest_and_input_untouched(city):
    weights = weight_table(city, minute=100)
    original = list(weights)
    routes = alternative_routes(city.adjacency, weights, (3, 4), (20, 25), 3, 1.5)
    assert weights == original
    best = dijkstra_path(city.adjacency, weights, (3, 4), (20, 25))
    assert routes[0].cost == pytest.approx(best.cost)
    assert all(r.cost >= routes[0].cost - 1e-9 for r in routes)


# --- 路況 -------------------------------------------------------------------


def test_traffic_factor_range_and_deterministic():
    values = [traffic_factor(e, m) for e in range(500) for m in range(0, 120, 7)]
    assert all(1.0 <= v <= 2.0 for v in values)
    assert max(values) > 1.9 and min(values) < 1.1
    assert traffic_factor(42, 7) == traffic_factor(42, 7)


def test_weight_table_changes_with_minute(city):
    a = weight_table(city, minute=10)
    b = weight_table(city, minute=11)
    assert len(a) == len(city.edges)
    assert a != b
    for w, base in zip(a, city.base_s):
        assert base <= w <= 2 * base


# --- 派單 -------------------------------------------------------------------


def _order(city, address=""):
    r = city.restaurants[0]
    return Order(restaurant_id=r["id"], items=[(r["menu"][0]["id"], 2), (r["menu"][1]["id"], 1)], address=address)


def test_chosen_candidate_has_lowest_score(city):
    for seed in range(10):
        plan = plan_order(_order(city), NOW, random.Random(seed), city=city, params=PARAMS)
        assert len(plan.candidates) == PARAMS.candidates
        assert all(plan.chosen.score <= c.score for c in plan.candidates)
        assert plan.chosen in plan.candidates


def test_candidates_are_nearest_riders(city):
    plan = plan_order(_order(city), NOW, random.Random(3), city=city, params=PARAMS)
    cand = {c.rider["id"] for c in plan.candidates}
    worst_cand = max(plan.rider_dist[i] for i in cand)
    others = [d for i, d in plan.rider_dist.items() if i not in cand]
    assert len(plan.rider_dist) == PARAMS.riders
    assert all(d >= worst_cand for d in others)


def test_riders_within_radius(city):
    plan = plan_order(_order(city), NOW, random.Random(5), city=city, params=PARAMS)
    rx, ry = city.restaurants[0]["node"]
    for rider in plan.riders:
        x, y = rider["node"]
        assert PARAMS.rider_min_radius <= abs(x - rx) + abs(y - ry) <= PARAMS.rider_radius
        assert 0 <= x < city.size and 0 <= y < city.size


@pytest.mark.parametrize("seed", range(20))
def test_schedule_order(city, seed):
    result = dispatch_order(_order(city), NOW + seed * 600, random.Random(seed), city=city, params=PARAMS)
    s = result["schedule"]
    assert s["arrive_restaurant_s"] <= s["pickup_s"] < s["deliver_s"]
    assert s["pickup_s"] >= 12 * 60  # 紅燒牛肉麵備餐 12 分鐘
    assert result["eta_minutes"] * 60 >= s["deliver_s"]


def test_schedule_order_when_customer_at_restaurant(city):
    r = city.restaurants[0]
    address = next(f"a{i}" for i in range(100_000) if city.address_to_node(f"a{i}") == r["node"])
    s = dispatch_order(_order(city, address), NOW, random.Random(0), city=city, params=PARAMS)["schedule"]
    assert s["arrive_restaurant_s"] <= s["pickup_s"] < s["deliver_s"]


def test_same_seed_and_now_same_result(city):
    a = dispatch_order(_order(city), NOW, random.Random(9), city=city, params=PARAMS)
    b = dispatch_order(_order(city), NOW, random.Random(9), city=city, params=PARAMS)
    assert a == b
    c = dispatch_order(_order(city), NOW, random.Random(10), city=city, params=PARAMS)
    assert a != c


def test_result_fields(city):
    r = city.restaurants[0]
    result = dispatch_order(_order(city, "台北市信義路五段7號"), NOW, random.Random(1), city=city, params=PARAMS)
    assert set(result) == {
        "restaurant", "customer_node", "total_price", "rider", "route",
        "schedule", "eta_minutes", "candidates_evaluated",
    }
    assert result["restaurant"] == {"id": r["id"], "name": r["name"], "node": list(r["node"])}
    assert result["customer_node"] == list(city.address_to_node("台北市信義路五段7號"))
    assert result["total_price"] == r["menu"][0]["price"] * 2 + r["menu"][1]["price"]
    assert set(result["rider"]) == {"id", "name"}
    assert result["candidates_evaluated"] == PARAMS.candidates
    to_r, to_c = result["route"]["to_restaurant"], result["route"]["to_customer"]
    assert to_r[-1] == list(r["node"]) and to_c[0] == list(r["node"])
    assert to_c[-1] == result["customer_node"]


def test_no_address_uses_rng_for_customer(city):
    nodes = {
        tuple(dispatch_order(_order(city), NOW, random.Random(s), city=city, params=PARAMS)["customer_node"])
        for s in range(10)
    }
    assert len(nodes) > 1


@pytest.mark.parametrize(
    "order",
    [
        Order(restaurant_id="nope", items=[("r1-m1", 1)]),
        Order(restaurant_id="r1", items=[("r2-m1", 1)]),
        Order(restaurant_id="r1", items=[("r1-m99", 1)]),
        Order(restaurant_id="r1", items=[("r1-m1", 0)]),
        Order(restaurant_id="r1", items=[]),
    ],
)
def test_invalid_order_rejected(city, order):
    with pytest.raises(InvalidOrder):
        dispatch_order(order, NOW, random.Random(0), city=city, params=PARAMS)


def test_params_from_env(monkeypatch):
    monkeypatch.setenv("RIDERS", "12")
    monkeypatch.setenv("CANDIDATES", "2")
    monkeypatch.setenv("ALT_PENALTY", "2.5")
    p = Params.from_env()
    assert (p.riders, p.candidates, p.alt_penalty) == (12, 2, 2.5)


def test_candidates_capped_by_riders(city):
    params = Params(riders=3, rider_radius=15, candidates=5, alt_routes=2, alt_penalty=1.5, reliability_weight=0.5)
    result = dispatch_order(_order(city), NOW, random.Random(0), city=city, params=params)
    assert result["candidates_evaluated"] == 3
