"""量測派單運算時間：python -m dispatch.bench

以 100 筆帶種子的訂單執行 dispatch_order，輸出平均 compute_ms。
"""

import random
import statistics
import time

from dispatch import config
from dispatch.engine import Order, dispatch_order
from shared.citymap import get_city

N = 100
NOW = 1_760_000_000.0


def make_order(city, rng: random.Random) -> Order:
    restaurant = rng.choice(city.restaurants)
    items = [(item["id"], rng.randint(1, 3)) for item in rng.sample(restaurant["menu"], rng.randint(1, 3))]
    address = f"bench-{rng.randrange(10**6)}" if rng.random() < 0.5 else ""
    return Order(restaurant_id=restaurant["id"], items=items, address=address)


def main() -> None:
    city = get_city()
    timings = []
    for seed in range(N):
        rng = random.Random(seed)
        order = make_order(city, rng)
        start = time.perf_counter()
        dispatch_order(order, NOW + seed * 60, rng, city=city)
        timings.append((time.perf_counter() - start) * 1000)

    timings.sort()
    print(f"city={city.size}x{city.size} params={config.PARAMS}")
    print(
        f"orders={N} avg compute_ms={statistics.mean(timings):.1f} "
        f"p50={timings[N // 2]:.1f} p95={timings[int(N * 0.95)]:.1f} max={timings[-1]:.1f}"
    )


if __name__ == "__main__":
    main()
