"""web：前端、地圖與設定、回報收集、SSE 指標推送（README 5.2）。

指標只存在記憶體，必須以單一 process 執行。
"""

import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from web import config
from web.metrics import Metrics
from shared.citymap import get_city

STATIC_DIR = Path(__file__).parent / "static"

Target = Literal["fixed", "auto"]
Outcome = Literal["ok", "timeout", "busy", "error"]


class OrderResult(BaseModel):
    target: Target
    outcome: Outcome
    latency_ms: float = Field(ge=0)
    instance_id: str | None = None


class OrderReport(BaseModel):
    order_id: str
    results: list[OrderResult]


class TargetSummary(BaseModel):
    ok: int = Field(0, ge=0)
    timeout: int = Field(0, ge=0)
    busy: int = Field(0, ge=0)
    error: int = Field(0, ge=0)
    latency_samples_ms: list[float] = []
    instance_ids: list[str] = []


class LoadtestReport(BaseModel):
    targets: dict[Target, TargetSummary]


class Broadcaster:
    """每個 SSE 連線一個佇列；慢的連線只保留最新幾筆。"""

    def __init__(self, maxsize: int = 5):
        self._queues: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def publish(self, message: str) -> None:
        for q in self._queues:
            if q.full():
                q.get_nowait()
            q.put_nowait(message)


def _sse(data: str) -> str:
    return f"event: snapshot\ndata: {data}\n\n"


def _snapshot_json(metrics: Metrics, now: float | None = None) -> str:
    return json.dumps(metrics.snapshot(now), separators=(",", ":"))


async def _pump(metrics: Metrics, broadcaster: Broadcaster) -> None:
    """對齊整秒，每秒計算一次快照並廣播；沒有連線時不計算。"""
    target = math.floor(time.time()) + 1
    while True:
        await asyncio.sleep(max(0.0, target - time.time()))
        if broadcaster.subscriber_count:
            broadcaster.publish(_snapshot_json(metrics, target))
        target += 1
        if target < time.time():
            target = math.floor(time.time()) + 1


def create_app() -> FastAPI:
    metrics = Metrics()
    broadcaster = Broadcaster()
    map_body = json.dumps(get_city().to_map_json(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    settings = {"fixed_url": config.FIXED_URL, "auto_url": config.AUTO_URL, "timeout_ms": config.TIMEOUT_MS}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_pump(metrics, broadcaster))
        yield
        task.cancel()

    app = FastAPI(title="web", lifespan=lifespan)
    app.state.metrics = metrics
    app.state.broadcaster = broadcaster

    @app.get("/api/config")
    async def get_config() -> dict:
        return settings

    @app.get("/api/map")
    async def get_map() -> Response:
        return Response(map_body, media_type="application/json")

    @app.post("/api/reports/order")
    async def report_order(report: OrderReport) -> dict:
        metrics.record_order([r.model_dump() for r in report.results])
        return {"ok": True}

    @app.post("/api/reports/loadtest")
    async def report_loadtest(report: LoadtestReport) -> dict:
        metrics.record_loadtest({t: s.model_dump() for t, s in report.targets.items()})
        return {"ok": True}

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        async def events():
            q = broadcaster.subscribe()
            try:
                yield _sse(_snapshot_json(metrics))
                while True:
                    yield _sse(await q.get())
            finally:
                broadcaster.unsubscribe(q)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
