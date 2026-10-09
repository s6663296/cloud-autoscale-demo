import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from web import config
from web.main import create_app
from shared.citymap import get_city

ORDER_REPORT = {
    "order_id": "uuid",
    "results": [
        {"target": "fixed", "outcome": "timeout", "latency_ms": 10000, "instance_id": None},
        {"target": "auto", "outcome": "ok", "latency_ms": 412, "instance_id": "00bf4bf0..."},
    ],
}

LOADTEST_REPORT = {
    "targets": {
        "fixed": {
            "ok": 8, "timeout": 52, "busy": 0, "error": 0,
            "latency_samples_ms": [10000, 9873],
            "instance_ids": ["00a1..."],
        },
        "auto": {"ok": 60, "timeout": 0, "busy": 0, "error": 0, "latency_samples_ms": [388], "instance_ids": ["00bf...", "00c2..."]},
    }
}


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def test_order_report_accepts_readme_example(client, app):
    assert client.post("/api/reports/order", json=ORDER_REPORT).status_code == 200
    snap = app.state.metrics.snapshot()
    assert snap["students"] == {"orders": 1, "fixed_ok": 0, "auto_ok": 1}
    assert snap["targets"]["auto"]["instances"] == 1


def test_loadtest_report_accepts_readme_example(client, app):
    assert client.post("/api/reports/loadtest", json=LOADTEST_REPORT).status_code == 200
    snap = app.state.metrics.snapshot()
    assert snap["targets"]["fixed"]["failures"]["timeout"] == 52
    assert snap["targets"]["auto"]["instances"] == 2
    assert snap["students"]["orders"] == 0


def test_loadtest_report_with_single_target(client):
    body = {"targets": {"auto": {"ok": 1, "latency_samples_ms": [5]}}}
    assert client.post("/api/reports/loadtest", json=body).status_code == 200


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/reports/order", {"order_id": "x", "results": [{"target": "nope", "outcome": "ok", "latency_ms": 1}]}),
        ("/api/reports/order", {"order_id": "x", "results": [{"target": "auto", "outcome": "great", "latency_ms": 1}]}),
        ("/api/reports/loadtest", {"targets": {"other": {"ok": 1}}}),
        ("/api/reports/loadtest", {"targets": {"auto": {"ok": -1}}}),
    ],
)
def test_reports_reject_malformed(client, path, body):
    assert client.post(path, json=body).status_code == 422


def test_config_from_env(monkeypatch):
    monkeypatch.setattr(config, "FIXED_URL", "https://fixed.example")
    monkeypatch.setattr(config, "AUTO_URL", "https://auto.example")
    monkeypatch.setattr(config, "TIMEOUT_MS", 50)
    monkeypatch.setattr(config, "TRACK_INTERVAL_MS", 1500)
    with TestClient(create_app()) as c:
        assert c.get("/api/config").json() == {
            "fixed_url": "https://fixed.example", "auto_url": "https://auto.example", "timeout_ms": 50,
            "track_interval_ms": 1500,
        }


def test_map(client):
    res = client.get("/api/map")
    assert res.status_code == 200
    assert res.json() == get_city().to_map_json()


def test_index_and_static(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert client.get("/static/index.html").status_code == 200


# --- SSE（以真實的 uvicorn 驗證串流）----------------------------------------


@pytest.fixture()
def live_server():
    app = create_app()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.05)
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _read_events(response, n):
    events, current = [], {}
    for line in response.iter_lines():
        if line == "":
            if current:
                events.append((time.monotonic(), current))
                current = {}
                if len(events) == n:
                    return events
            continue
        field, _, value = line.partition(":")
        current[field] = value.strip()
    return events


def test_stream_sends_snapshot_every_second(live_server):
    app, base = live_server
    with httpx.stream("GET", f"{base}/api/stream", timeout=10) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = _read_events(res, 4)
    assert [e["event"] for _, e in events] == ["snapshot"] * 4
    nows = [json.loads(e["data"])["now"] for _, e in events]
    assert set(json.loads(events[0][1]["data"])) == {"now", "targets", "students", "series"}
    # 第一筆為連線時立即送出，之後每秒一筆
    gaps = [b[0] - a[0] for a, b in zip(events[1:], events[2:])]
    assert all(0.7 < g < 1.3 for g in gaps), gaps
    assert nows[-1] - nows[1] == 2


def test_stream_unsubscribes_on_disconnect(live_server):
    app, base = live_server
    with httpx.stream("GET", f"{base}/api/stream", timeout=10) as res:
        _read_events(res, 1)
        assert app.state.broadcaster.subscriber_count == 1
    deadline = time.time() + 5
    while app.state.broadcaster.subscriber_count:
        assert time.time() < deadline, "subscriber not removed after disconnect"
        time.sleep(0.1)


def test_track_report_accepts_readme_example(client, app):
    body = {"target": "auto", "outcome": "ok", "latency_ms": 18, "instance_id": "00bf4bf0..."}
    assert client.post("/api/reports/track", json=body).status_code == 200
    snap = app.state.metrics.snapshot()
    assert snap["targets"]["auto"]["instances"] == 1
    assert snap["students"]["orders"] == 0


def test_track_report_rejects_malformed(client):
    assert client.post("/api/reports/track", json={"target": "x", "outcome": "ok", "latency_ms": 1}).status_code == 422
