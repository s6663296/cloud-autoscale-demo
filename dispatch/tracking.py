"""配送追蹤（README 4.3）。不依賴 FastAPI，now 由呼叫端注入。

追蹤狀態由用戶端保管、每次請求送回，因此任何一台 dispatch 都能推進同一筆配送。
外送員在模擬時間中移動：模擬秒數 = (now − dispatched_at) × time_scale。
"""

import math
from typing import Callable

from dispatch import config
from dispatch.engine import weight_table
from dispatch.routing import dijkstra_path
from shared.citymap import CityMap, Node, get_city

VERSION = 1
PHASES = ("to_restaurant", "waiting", "to_customer", "delivered")


class InvalidTracking(ValueError):
    pass


def start_tracking(result: dict, now: float) -> dict:
    """由派單結果建立初始追蹤狀態。"""
    return {
        "v": VERSION,
        "dispatched_at": now,
        "sim_s": 0.0,
        "phase": "to_restaurant",
        "node": list(result["route"]["to_restaurant"][0]),
        "next": None,
        "edge_frac": 0.0,
        "restaurant": list(result["restaurant"]["node"]),
        "customer": list(result["customer_node"]),
        "pickup_s": float(result["schedule"]["pickup_s"]),
        "delivered_s": None,
    }


def advance(
    state: dict,
    now: float,
    *,
    city: CityMap | None = None,
    time_scale: float | None = None,
    weights_fn: Callable[[int], list[float]] | None = None,
) -> tuple[dict, dict]:
    """把外送員推進到 now 對應的模擬時間，回傳 (新狀態, 顯示用資料)。"""
    city = city or get_city()
    time_scale = time_scale or config.TIME_SCALE
    s = _validate(state, city)
    weights_fn = weights_fn or (lambda minute: weight_table(city, minute))
    weights = weights_fn(int((s["dispatched_at"] + s["sim_s"]) // 60))

    target = max(s["sim_s"], (now - s["dispatched_at"]) * time_scale)
    t = s["sim_s"]
    trail = [_position(s)]
    plan: list[Node] | None = None  # 本次請求規劃的路線，起點為規劃當下的路口

    while s["phase"] != "delivered":
        if s["phase"] == "waiting":
            if t >= s["pickup_s"]:
                s["phase"] = "to_customer"
                plan = None
                continue
            if t >= target:
                break
            t = min(target, s["pickup_s"])
            continue

        stop = s["restaurant"] if s["phase"] == "to_restaurant" else s["customer"]
        if s["next"] is None:
            if s["node"] == stop:
                if s["phase"] == "to_restaurant":
                    s["phase"] = "waiting"
                else:
                    s["phase"] = "delivered"
                    s["delivered_s"] = t
                plan = None
                continue
            if t >= target:
                break
            # 重新規劃：依本次的路況從目前路口找最佳路線，路況改變時就會改道
            if plan is None or plan[-1] != stop or s["node"] not in plan:
                plan = dijkstra_path(city.adjacency, weights, s["node"], stop).nodes
            s["next"] = plan[plan.index(s["node"]) + 1]
            s["edge_frac"] = 0.0

        cost = weights[_edge(city, s["node"], s["next"])]
        need = (1 - s["edge_frac"]) * cost
        if t + need <= target:
            t += need
            s["node"], s["next"], s["edge_frac"] = s["next"], None, 0.0
            trail.append(_position(s))
        else:
            s["edge_frac"] += (target - t) / cost
            t = target
            break

    s["sim_s"] = target
    view = _view(s, city, weights, plan, trail)
    return _dump(s), view


# --- 顯示資料與預估送達 -----------------------------------------------------


def _view(s: dict, city: CityMap, weights: list[float], plan: list[Node] | None, trail: list) -> dict:
    position = _position(s)
    if trail[-1] != position:
        trail.append(position)
    t = s["sim_s"]
    if s["phase"] == "delivered":
        return _view_dict(s, position, trail, [position], 0)

    def path_from(start: Node, stop: Node) -> list[Node]:
        if plan is not None and plan[-1] == stop and start in plan:
            return plan[plan.index(start):]
        return dijkstra_path(city.adjacency, weights, start, stop).nodes

    def cost_of(nodes: list[Node]) -> float:
        return sum(weights[_edge(city, a, b)] for a, b in zip(nodes, nodes[1:]))

    restaurant, customer = s["restaurant"], s["customer"]
    if s["phase"] == "waiting":
        route = [position]
        arrival = t
    else:
        stop = restaurant if s["phase"] == "to_restaurant" else customer
        start = s["next"] if s["next"] is not None else s["node"]
        nodes = path_from(start, stop)
        head = (1 - s["edge_frac"]) * weights[_edge(city, s["node"], s["next"])] if s["next"] is not None else 0.0
        arrival = t + head + cost_of(nodes)
        route = ([position] if s["next"] is not None else []) + [list(n) for n in nodes]

    if s["phase"] == "to_customer":
        eta = arrival - t
    else:
        to_customer = dijkstra_path(city.adjacency, weights, restaurant, customer).cost
        eta = max(arrival, s["pickup_s"]) - t + to_customer
    return _view_dict(s, position, trail, route, eta)


def _view_dict(s: dict, position, trail, route, eta: float) -> dict:
    return {
        "phase": s["phase"],
        "position": position,
        "trail": [list(p) for p in trail],
        "route": [list(p) for p in route],
        "eta_s": max(0, round(eta)),
        "sim_s": round(s["sim_s"]),
    }


def _position(s: dict) -> list:
    x, y = s["node"]
    if s["next"] is None or s["edge_frac"] == 0:
        return [x, y]
    nx, ny = s["next"]
    f = s["edge_frac"]
    return [round(x + (nx - x) * f, 3), round(y + (ny - y) * f, 3)]


# --- 驗證與序列化 -----------------------------------------------------------


def _edge(city: CityMap, a: Node, b: Node) -> int:
    for nb, e in city.adjacency[a]:
        if nb == b:
            return e
    raise InvalidTracking(f"{a} and {b} are not connected")


def _node(value, city: CityMap, name: str) -> Node:
    if not (isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
        raise InvalidTracking(f"{name} must be [x, y]")
    x, y = value
    if not (0 <= x < city.size and 0 <= y < city.size):
        raise InvalidTracking(f"{name} out of map")
    return (x, y)


def _number(value, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidTracking(f"{name} must be a number")
    if minimum is not None and value < minimum:
        raise InvalidTracking(f"{name} must be >= {minimum}")
    return float(value)


def _validate(state, city: CityMap) -> dict:
    if not isinstance(state, dict):
        raise InvalidTracking("tracking must be an object")
    try:
        if state["v"] != VERSION:
            raise InvalidTracking("unsupported tracking version")
        if state["phase"] not in PHASES:
            raise InvalidTracking("invalid phase")
        s = {
            "dispatched_at": _number(state["dispatched_at"], "dispatched_at"),
            "sim_s": _number(state["sim_s"], "sim_s", 0),
            "phase": state["phase"],
            "node": _node(state["node"], city, "node"),
            "next": None if state["next"] is None else _node(state["next"], city, "next"),
            "edge_frac": _number(state["edge_frac"], "edge_frac", 0),
            "restaurant": _node(state["restaurant"], city, "restaurant"),
            "customer": _node(state["customer"], city, "customer"),
            "pickup_s": _number(state["pickup_s"], "pickup_s", 0),
            "delivered_s": None if state.get("delivered_s") is None else _number(state["delivered_s"], "delivered_s", 0),
        }
    except KeyError as e:
        raise InvalidTracking(f"missing field {e}") from None
    if s["edge_frac"] >= 1 or (s["next"] is None and s["edge_frac"] != 0):
        raise InvalidTracking("invalid edge_frac")
    if s["next"] is not None:
        _edge(city, s["node"], s["next"])
    return s


def _dump(s: dict) -> dict:
    return {
        "v": VERSION,
        **{k: s[k] for k in ("dispatched_at", "sim_s", "phase")},
        "node": list(s["node"]),
        "next": None if s["next"] is None else list(s["next"]),
        "edge_frac": s["edge_frac"],
        "restaurant": list(s["restaurant"]),
        "customer": list(s["customer"]),
        "pickup_s": s["pickup_s"],
        "delivered_s": s["delivered_s"],
    }
