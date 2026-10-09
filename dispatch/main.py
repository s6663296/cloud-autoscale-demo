"""派單 API（README 5.1）。"""

import os
import random
import socket
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from dispatch import config
from dispatch.engine import InvalidOrder, Order, dispatch_order
from shared.citymap import get_city

METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/instance/id"


class OrderItem(BaseModel):
    item_id: str
    qty: int


class Customer(BaseModel):
    name: str = ""
    phone: str = ""
    address: str = ""
    note: str = ""


class OrderRequest(BaseModel):
    order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    restaurant_id: str
    items: list[OrderItem]
    customer: Customer = Field(default_factory=Customer)
    seed: int | None = None


def fetch_metadata_instance_id() -> str:
    res = httpx.get(METADATA_URL, headers={"Metadata-Flavor": "Google"}, timeout=1.0)
    res.raise_for_status()
    return res.text.strip()


def resolve_instance_id(fetch: Callable[[], str]) -> str:
    try:
        return fetch()
    except Exception:
        return f"local-{socket.gethostname()}-{os.getpid()}"


def create_app(allowed_origin: str = config.ALLOWED_ORIGIN) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.instance_id = resolve_instance_id(fetch_metadata_instance_id)
        get_city()  # 啟動時先建好路網，避免第一筆訂單變慢
        yield

    app = FastAPI(title="dispatch", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[allowed_origin] if allowed_origin else [],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.post("/api/orders")
    def create_order(req: OrderRequest) -> dict:
        # 以 order_id 為種子：同一筆訂單送往兩個版本得到相同結果，不同訂單之間仍為隨機
        rng = random.Random(req.seed if req.seed is not None else req.order_id)
        order = Order(
            restaurant_id=req.restaurant_id,
            items=[(item.item_id, item.qty) for item in req.items],
            address=req.customer.address,
        )
        start = time.perf_counter()
        try:
            result = dispatch_order(order, time.time(), rng)
        except InvalidOrder as e:
            raise HTTPException(status_code=422, detail=str(e))
        compute_ms = round((time.perf_counter() - start) * 1000, 1)
        return {
            "order_id": req.order_id,
            "service": config.SERVICE,
            "instance_id": app.state.instance_id,
            "compute_ms": compute_ms,
            **result,
        }

    @app.get("/api/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app


app = create_app()
