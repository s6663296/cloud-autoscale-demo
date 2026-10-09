"""量測派單運算時間：python -m dispatch.bench

以 100 筆帶種子的訂單執行 dispatch_order，輸出平均 compute_ms；
再把每筆訂單以 2 秒的追蹤間隔推進到送達，輸出追蹤請求的平均 compute_ms。
"""

import random
import statistics
import time

from dispatch import config
from dispatch.engine import Order, dispatch_order
from dispatch.tracking import advance, start_tracking
from shared.citymap import get_city

N = 100
NOW = 1_760_000_000.0
TRACK_INTERVAL_S = 2.0


def make_order(city, rng: random.Random) -> Order:
    restaurant = rng.choice(city.restaurants)
    items = [(item["id"], rng.randint(1, 3)) for item in rng.sample(restaurant["menu"], rng.randint(1, 3))]
    address = f"bench-{rng.randrange(10**6)}" if rng.random() < 0.5 else ""
    return Order(restaurant_id=restaurant["id"], items=items, address=address)


def summarize(label: str, timings: list[float]) -> str:
    timings = sorted(timings)
    n = len(timings)
    return (
        f"{label}={n} avg compute_ms={statistics.mean(timings):.1f} "
        f"p50={timings[n // 2]:.1f} p95={timings[int(n * 0.95)]:.1f} max={timings[-1]:.1f}"
    )


def main() -> None:
    city = get_city()
    order_ms, track_ms, tracks_per_order = [], [], []
    for seed in range(N):
        rng = random.Random(seed)
        order = make_order(city, rng)
        now = NOW + seed * 60
        start = time.perf_counter()
        result = dispatch_order(order, now, rng, city=city)
        order_ms.append((time.perf_counter() - start) * 1000)

        state, calls = start_tracking(result, now), 0
        while True:
            now += TRACK_INTERVAL_S
            start = time.perf_counter()
            state, view = advance(state, now, city=city)
            track_ms.append((time.perf_counter() - start) * 1000)
            calls += 1
            if view["phase"] == "delivered":
                break
        tracks_per_order.append(calls)

    print(f"city={city.size}x{city.size} params={config.PARAMS} time_scale={config.TIME_SCALE}")
    print(summarize("orders", order_ms))
    print(summarize("tracks", track_ms))
    per_order = statistics.mean(order_ms) + statistics.mean(track_ms) * statistics.mean(tracks_per_order)
    print(f"tracks/order avg={statistics.mean(tracks_per_order):.1f} → 每筆訂單總運算約 {per_order:.0f} ms")


if __name__ == "__main__":
    main()
