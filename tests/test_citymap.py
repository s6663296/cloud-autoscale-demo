import json
from collections import deque

import pytest

from shared.citymap import ARTERIAL_S, CITY_SEED, ROAD_S, build_city


@pytest.fixture(scope="module")
def city60():
    return build_city(60)


def _total_edges(size):
    return 2 * size * (size - 1)


def _reachable(city, start):
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nb, _ in city.adjacency[node]:
            if nb not in seen:
                seen.add(nb)
                queue.append(nb)
    return seen


def test_same_parameters_produce_identical_city():
    a = build_city(20)
    b = build_city(20)
    assert a.edges == b.edges
    assert a.base_s == b.base_s
    assert a.to_map_json() == b.to_map_json()


def test_different_seed_produces_different_city():
    assert build_city(20, seed=CITY_SEED).edges != build_city(20, seed=CITY_SEED + 1).edges


@pytest.mark.parametrize("size", [5, 20, 60])
def test_city_is_connected(size, city60):
    city = city60 if size == 60 else build_city(size)
    assert len(_reachable(city, (0, 0))) == size * size


def test_adjacency_matches_edges(city60):
    for i, (u, v) in enumerate(city60.edges):
        assert (v, i) in city60.adjacency[u]
        assert (u, i) in city60.adjacency[v]
    assert sum(len(n) for n in city60.adjacency.values()) == 2 * len(city60.edges)


@pytest.mark.parametrize("size", [20, 60])
def test_closed_ratio_about_ten_percent(size, city60):
    city = city60 if size == 60 else build_city(size)
    ratio = len(city.closed) / _total_edges(size)
    assert 0.08 <= ratio <= 0.12
    assert len(city.closed) + len(city.edges) == _total_edges(size)
    assert not set(city.closed) & set(city.edges)


def test_edges_are_canonical_unit_segments(city60):
    for u, v in city60.edges + city60.closed:
        assert u < v
        assert abs(u[0] - v[0]) + abs(u[1] - v[1]) == 1


def test_arterials_are_complete_lines_with_lower_cost(city60):
    rows, cols = city60.arterial_rows, city60.arterial_cols
    assert rows and cols
    open_edges = set(city60.edges)
    for y in rows:
        for x in range(59):
            assert ((x, y), (x + 1, y)) in open_edges
    for x in cols:
        for y in range(59):
            assert ((x, y), (x, y + 1)) in open_edges
    for (u, v), s in zip(city60.edges, city60.base_s):
        on_arterial = (u[1] == v[1] and u[1] in rows) or (u[0] == v[0] and u[0] in cols)
        assert s == (ARTERIAL_S if on_arterial else ROAD_S)


def test_six_restaurants_on_distinct_nodes_with_menus(city60):
    rs = city60.restaurants
    assert [r["id"] for r in rs] == ["r1", "r2", "r3", "r4", "r5", "r6"]
    nodes = [r["node"] for r in rs]
    assert len(set(nodes)) == 6
    assert all(0 <= x < 60 and 0 <= y < 60 for x, y in nodes)
    item_ids = []
    for r in rs:
        assert 4 <= len(r["menu"]) <= 6
        for item in r["menu"]:
            assert set(item) == {"id", "name", "price", "prep_min"}
            assert item["id"].startswith(r["id"] + "-m")
            assert item["price"] > 0 and item["prep_min"] > 0
            item_ids.append(item["id"])
    assert len(item_ids) == len(set(item_ids))


def test_restaurant_lookup(city60):
    assert city60.restaurant("r3")["id"] == "r3"
    assert city60.restaurant("nope") is None


def test_address_maps_to_same_node(city60):
    node = city60.address_to_node("台北市大安區羅斯福路四段1號")
    assert node == city60.address_to_node("台北市大安區羅斯福路四段1號")
    assert node == build_city(60).address_to_node("台北市大安區羅斯福路四段1號")
    x, y = node
    assert 0 <= x < 60 and 0 <= y < 60


def test_different_addresses_spread_over_nodes(city60):
    nodes = {city60.address_to_node(f"地址{i}") for i in range(100)}
    assert len(nodes) > 90


def test_map_json_serializable_and_small(city60):
    data = city60.to_map_json()
    text = json.dumps(data, ensure_ascii=False)
    assert len(text.encode("utf-8")) < 200 * 1024
    assert data["size"] == 60
    assert data["base_s"] == {"road": ROAD_S, "arterial": ARTERIAL_S}
    assert len(data["closed"]) == len(city60.closed)
    assert data["arterials"] == {"rows": city60.arterial_rows, "cols": city60.arterial_cols}
    assert [r["id"] for r in data["restaurants"]] == ["r1", "r2", "r3", "r4", "r5", "r6"]
    assert json.loads(text) == data


def test_get_city_uses_configured_size(monkeypatch):
    from shared import citymap, config

    monkeypatch.setattr(config, "CITY_GRID_SIZE", 7)
    citymap.get_city.cache_clear()
    try:
        assert citymap.get_city().size == 7
        assert citymap.get_city() is citymap.get_city()
    finally:
        citymap.get_city.cache_clear()
