"""單筆訂單派單流程（README 4.2）。不依賴 FastAPI，now 與 rng 由呼叫端注入。"""

import math
import random
from dataclasses import dataclass

from dispatch import config
from dispatch.config import Params
from dispatch.routing import Route, alternative_routes, dijkstra_all
from shared.citymap import CityMap, Node, get_city

TRAFFIC_PERIOD_MIN = 30

RIDER_NAMES = [
    "陳志明", "林怡君", "黃俊傑", "張雅婷", "李建宏", "王淑芬", "吳宗翰", "劉佳穎",
    "蔡承恩", "楊詩涵", "許家豪", "鄭雅雯", "謝冠宇", "郭欣怡", "洪柏翰", "曾郁婷",
]


class InvalidOrder(ValueError):
    pass


@dataclass
class Order:
    restaurant_id: str
    items: list[tuple[str, int]]
    address: str = ""


@dataclass
class Candidate:
    rider: dict
    to_restaurant: list[Route]
    arrive_s: float
    pickup_s: float
    deliver_s: float
    score: float


@dataclass
class Plan:
    restaurant: dict
    customer_node: Node
    total_price: int
    riders: list[dict]
    rider_dist: dict[str, float]
    to_customer: list[Route]
    candidates: list[Candidate]
    chosen: Candidate


def traffic_factor(edge_index: int, minute: int) -> float:
    """路況係數 1.0–2.0：每條路段有固定相位，隨分鐘數以正弦變化。"""
    phase = ((edge_index * 2654435761) & 0xFFFFFFFF) / 2**32
    return 1.5 + 0.5 * math.sin(2 * math.pi * (phase + minute / TRAFFIC_PERIOD_MIN))


def weight_table(city: CityMap, minute: int) -> list[float]:
    return [base * traffic_factor(i, minute) for i, base in enumerate(city.base_s)]


def dispatch_order(order: Order, now: float, rng: random.Random, *, city=None, params=None) -> dict:
    plan = plan_order(order, now, rng, city=city, params=params)
    c = plan.chosen
    arrive = round(c.arrive_s)
    pickup = max(arrive, round(c.pickup_s))
    # 顧客就在店家路口時送餐路線為 0 秒，仍保證 pickup < deliver
    deliver = max(pickup + 1, round(c.deliver_s))
    r = plan.restaurant
    return {
        "restaurant": {"id": r["id"], "name": r["name"], "node": list(r["node"])},
        "customer_node": list(plan.customer_node),
        "total_price": plan.total_price,
        "rider": {"id": c.rider["id"], "name": c.rider["name"]},
        "route": {
            "to_restaurant": [list(n) for n in c.to_restaurant[0].nodes],
            "to_customer": [list(n) for n in plan.to_customer[0].nodes],
        },
        "schedule": {"arrive_restaurant_s": arrive, "pickup_s": pickup, "deliver_s": deliver},
        "eta_minutes": math.ceil(deliver / 60),
        "candidates_evaluated": len(plan.candidates),
    }


def plan_order(order: Order, now: float, rng: random.Random, *, city=None, params=None) -> Plan:
    city = city or get_city()
    params = params or config.PARAMS
    restaurant, total_price, prep_s = _validate(city, order)
    r_node = restaurant["node"]
    if order.address.strip():
        customer_node = city.address_to_node(order.address)
    else:
        customer_node = (rng.randrange(city.size), rng.randrange(city.size))
    weights = weight_table(city, int(now // 60))
    riders = _spawn_riders(rng, city, r_node, params)

    # 無向圖：從店家做一次完整搜尋，即得每位外送員到店家的距離
    dist, _ = dijkstra_all(city.adjacency, weights, r_node)
    rider_dist = {rd["id"]: dist[rd["node"]] for rd in riders}
    nearest = sorted(riders, key=lambda rd: rider_dist[rd["id"]])[: params.candidates]

    to_customer = alternative_routes(
        city.adjacency, weights, r_node, customer_node, params.alt_routes, params.alt_penalty
    )
    best_c = min(rt.cost for rt in to_customer)
    worst_c = max(rt.cost for rt in to_customer)

    candidates = []
    for rider in nearest:
        to_r = alternative_routes(
            city.adjacency, weights, rider["node"], r_node, params.alt_routes, params.alt_penalty
        )
        arrive = min(rt.cost for rt in to_r)
        pickup = max(arrive, prep_s)
        deliver = pickup + best_c
        worst = max(max(rt.cost for rt in to_r), prep_s) + worst_c
        score = deliver + params.reliability_weight * (worst - deliver)
        candidates.append(Candidate(rider, to_r, arrive, pickup, deliver, score))

    chosen = min(candidates, key=lambda c: c.score)
    return Plan(restaurant, customer_node, total_price, riders, rider_dist, to_customer, candidates, chosen)


def _validate(city: CityMap, order: Order) -> tuple[dict, int, float]:
    restaurant = city.restaurant(order.restaurant_id)
    if restaurant is None:
        raise InvalidOrder(f"unknown restaurant: {order.restaurant_id}")
    if not order.items:
        raise InvalidOrder("order has no items")
    menu = {item["id"]: item for item in restaurant["menu"]}
    total, prep_min = 0, 0
    for item_id, qty in order.items:
        item = menu.get(item_id)
        if item is None:
            raise InvalidOrder(f"unknown item: {item_id}")
        if qty < 1:
            raise InvalidOrder(f"invalid qty for {item_id}: {qty}")
        total += item["price"] * qty
        prep_min = max(prep_min, item["prep_min"])
    return restaurant, total, prep_min * 60.0


def _spawn_riders(rng: random.Random, city: CityMap, center: Node, params: Params) -> list[dict]:
    """在店家曼哈頓距離 rider_radius 內隨機產生外送員。"""
    cx, cy = center
    rad = params.rider_radius
    riders = []
    while len(riders) < params.riders:
        dx, dy = rng.randint(-rad, rad), rng.randint(-rad, rad)
        x, y = cx + dx, cy + dy
        if abs(dx) + abs(dy) <= rad and 0 <= x < city.size and 0 <= y < city.size:
            riders.append({"id": f"rd-{len(riders) + 1}", "name": rng.choice(RIDER_NAMES), "node": (x, y)})
    return riders
