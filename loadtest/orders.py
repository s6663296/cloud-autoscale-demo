"""從 /api/map 的店家與菜單隨機產生訂單。"""

import random
import uuid


class OrderFactory:
    def __init__(self, map_json: dict, rng: random.Random, seed: int | None = None):
        self.restaurants = map_json["restaurants"]
        self.rng = rng
        self.seed = seed

    def next(self) -> dict:
        rng = self.rng
        restaurant = rng.choice(self.restaurants)
        menu = restaurant["menu"]
        items = rng.sample(menu, rng.randint(1, min(3, len(menu))))
        address = f"壓測路 {rng.randint(1, 999)} 號" if rng.random() < 0.5 else ""
        return {
            "order_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
            "restaurant_id": restaurant["id"],
            "items": [{"item_id": item["id"], "qty": rng.randint(1, 2)} for item in items],
            "customer": {"name": "", "phone": "", "address": address, "note": ""},
            "seed": self.seed,
        }
