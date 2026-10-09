import os
import socket

import pytest
from fastapi.testclient import TestClient

from dispatch import config, main

RESPONSE_FIELDS = {
    "order_id", "service", "instance_id", "compute_ms", "restaurant", "customer_node",
    "total_price", "rider", "route", "schedule", "eta_minutes", "candidates_evaluated", "tracking",
}
TRACK_FIELDS = {
    "order_id", "service", "instance_id", "compute_ms", "phase", "position", "trail", "route",
    "eta_s", "sim_s", "tracking",
}


def _no_metadata():
    raise OSError("metadata server unavailable")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main, "fetch_metadata_instance_id", _no_metadata)
    with TestClient(main.create_app(allowed_origin="https://web.example")) as c:
        yield c


def _order(**overrides):
    body = {
        "order_id": "11111111-2222-3333-4444-555555555555",
        "restaurant_id": "r1",
        "items": [{"item_id": "r1-m2", "qty": 1}],
        "customer": {"name": "", "phone": "", "address": "", "note": ""},
        "seed": None,
    }
    body.update(overrides)
    return body


def test_valid_order_returns_readme_fields(client):
    res = client.post("/api/orders", json=_order())
    assert res.status_code == 200
    data = res.json()
    assert set(data) == RESPONSE_FIELDS
    assert data["order_id"] == "11111111-2222-3333-4444-555555555555"
    assert data["service"] == config.SERVICE
    assert data["instance_id"] == f"local-{socket.gethostname()}-{os.getpid()}"
    assert isinstance(data["compute_ms"], float) and data["compute_ms"] > 0
    assert data["restaurant"]["id"] == "r1"
    assert set(data["schedule"]) == {"arrive_restaurant_s", "pickup_s", "deliver_s"}
    assert set(data["route"]) == {"to_restaurant", "to_customer"}
    assert set(data["rider"]) == {"id", "name"}


def test_same_seed_same_response_except_compute_ms(client):
    a = _without_compute_ms(client.post("/api/orders", json=_order(seed=42)))
    b = _without_compute_ms(client.post("/api/orders", json=_order(seed=42)))
    assert a == b


def _without_compute_ms(res):
    """去掉每次都不同的欄位：運算時間與追蹤狀態中的派單時刻。"""
    data = res.json()
    data.pop("compute_ms")
    data["tracking"].pop("dispatched_at")
    return data


def test_same_order_id_same_result_on_both_versions(client):
    """同一筆訂單（相同 order_id、未帶 seed）送往兩個版本，派單結果相同。"""
    body = _order(order_id="student-order-1")
    assert _without_compute_ms(client.post("/api/orders", json=body)) == _without_compute_ms(
        client.post("/api/orders", json=body)
    )


def test_different_order_ids_are_random(client):
    results = {
        tuple(client.post("/api/orders", json=_order(order_id=f"order-{i}")).json()["customer_node"])
        for i in range(8)
    }
    assert len(results) > 1


def test_seed_overrides_order_id(client):
    a = _without_compute_ms(client.post("/api/orders", json=_order(order_id="a", seed=5)))
    b = _without_compute_ms(client.post("/api/orders", json=_order(order_id="b", seed=5)))
    assert a.pop("order_id") == "a" and b.pop("order_id") == "b"
    assert a == b


def test_address_determines_customer_node(client):
    customer = {"address": "台北市中正區重慶南路一段122號"}
    a = client.post("/api/orders", json=_order(customer=customer)).json()
    b = client.post("/api/orders", json=_order(customer=customer)).json()
    assert a["customer_node"] == b["customer_node"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"restaurant_id": "r99"},
        {"items": [{"item_id": "r2-m1", "qty": 1}]},
        {"items": [{"item_id": "r1-m99", "qty": 1}]},
        {"items": [{"item_id": "r1-m1", "qty": 0}]},
        {"items": [{"item_id": "r1-m1", "qty": -2}]},
        {"items": []},
        {"items": [{"item_id": "r1-m1", "qty": "many"}]},
    ],
)
def test_invalid_order_returns_422(client, overrides):
    assert client.post("/api/orders", json=_order(**overrides)).status_code == 422


@pytest.mark.parametrize("customer", [None, {}, {"name": "小明"}])
def test_customer_fields_optional(client, customer):
    body = _order()
    if customer is None:
        del body["customer"]
    else:
        body["customer"] = customer
    assert client.post("/api/orders", json=body).status_code == 200


def test_minimal_body(client):
    res = client.post("/api/orders", json={"restaurant_id": "r3", "items": [{"item_id": "r3-m1", "qty": 2}]})
    assert res.status_code == 200
    assert res.json()["order_id"]


def test_healthz(client):
    res = client.get("/api/healthz")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def _preflight(client, origin):
    return client.options(
        "/api/orders",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type"},
    )


def test_cors_allows_only_configured_origin(client):
    assert _preflight(client, "https://web.example").headers.get("access-control-allow-origin") == "https://web.example"
    assert "access-control-allow-origin" not in _preflight(client, "https://evil.example").headers


def test_cors_accepts_comma_separated_origins(monkeypatch):
    monkeypatch.setattr(main, "fetch_metadata_instance_id", _no_metadata)
    app = main.create_app(allowed_origin="http://localhost:8000, http://127.0.0.1:8000")
    with TestClient(app) as c:
        for origin in ("http://localhost:8000", "http://127.0.0.1:8000"):
            assert _preflight(c, origin).headers.get("access-control-allow-origin") == origin
        assert "access-control-allow-origin" not in _preflight(c, "https://evil.example").headers


def test_instance_id_from_metadata():
    assert main.resolve_instance_id(lambda: "00bf4bf0abc") == "00bf4bf0abc"


def test_instance_id_fallback():
    assert main.resolve_instance_id(_no_metadata) == f"local-{socket.gethostname()}-{os.getpid()}"


# --- 配送追蹤 ---------------------------------------------------------------


class FakeClock:
    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _app(monkeypatch, clock):
    monkeypatch.setattr(main, "fetch_metadata_instance_id", _no_metadata)
    return TestClient(main.create_app(allowed_origin="https://web.example", clock=clock))


def test_order_returns_initial_tracking(client):
    data = client.post("/api/orders", json=_order(seed=3)).json()
    assert data["tracking"]["v"] == 1
    assert data["tracking"]["phase"] == "to_restaurant"
    assert data["tracking"]["node"] == data["route"]["to_restaurant"][0]


def test_track_advances_until_delivered(monkeypatch):
    clock = FakeClock()
    with _app(monkeypatch, clock) as c:
        order = c.post("/api/orders", json=_order(seed=3)).json()
        tracking, phases = order["tracking"], []
        for _ in range(500):
            clock.t += 2
            res = c.post("/api/track", json={"order_id": order["order_id"], "tracking": tracking})
            assert res.status_code == 200
            data = res.json()
            assert set(data) == TRACK_FIELDS
            assert data["order_id"] == order["order_id"]
            tracking = data["tracking"]
            phases.append(data["phase"])
            if data["phase"] == "delivered":
                break
    assert phases[-1] == "delivered"
    assert data["position"] == order["customer_node"] and data["eta_s"] == 0


def test_track_is_stateless_across_instances(monkeypatch):
    """兩個獨立的 app 交替處理同一筆配送，結果與單一 app 相同。"""
    clock = FakeClock()
    with _app(monkeypatch, clock) as a, _app(monkeypatch, clock) as b:
        order = a.post("/api/orders", json=_order(seed=5)).json()
        single, mixed = order["tracking"], order["tracking"]
        for i in range(12):
            clock.t += 2
            one = a.post("/api/track", json={"tracking": single}).json()
            other = (a if i % 2 else b).post("/api/track", json={"tracking": mixed}).json()
            for d in (one, other):
                d.pop("compute_ms")
            assert one == other
            single, mixed = one["tracking"], other["tracking"]


@pytest.mark.parametrize("tracking", [{}, {"v": 1}, "nope", None, {"v": 2, "phase": "to_restaurant"}])
def test_track_rejects_invalid_state(client, tracking):
    assert client.post("/api/track", json={"tracking": tracking}).status_code == 422
