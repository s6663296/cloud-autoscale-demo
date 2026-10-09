"""城市路網：格狀路網、封閉路段、主幹道、店家與菜單（README 4.1）。

所有輸出只由 size 與 seed 決定，dispatch 與 web 產生的結果完全一致。
"""

import hashlib
import math
import random
from collections import deque
from functools import lru_cache

from shared import config

CITY_SEED = 20261008
ROAD_S = 30
ARTERIAL_S = 15
CLOSED_RATIO = 0.10

Node = tuple[int, int]
Edge = tuple[Node, Node]

MENUS = [
    ("阿明牛肉麵", [
        ("紅燒牛肉麵", 180, 6), ("清燉牛肉麵", 190, 6), ("牛肉湯餃", 120, 5),
        ("燙青菜", 50, 2), ("滷味拼盤", 80, 2),
    ]),
    ("好食堂便當", [
        ("排骨便當", 110, 4), ("雞腿便當", 120, 5), ("控肉便當", 115, 4), ("魚排便當", 125, 4),
    ]),
    ("小巷鹹酥雞", [
        ("鹹酥雞", 70, 4), ("甜不辣", 40, 3), ("炸杏鮑菇", 60, 4),
        ("四季豆", 45, 2), ("雞排", 85, 4), ("炸花枝丸", 50, 3),
    ]),
    ("晨光早午餐", [
        ("蛋餅", 40, 2), ("總匯三明治", 65, 3), ("蘿蔔糕", 40, 2), ("鐵板麵", 60, 4), ("大冰奶", 35, 1),
    ]),
    ("義式小館", [
        ("番茄肉醬麵", 220, 7), ("青醬蛤蜊麵", 260, 8), ("瑪格麗特披薩", 280, 9), ("凱薩沙拉", 150, 3),
    ]),
    ("珍好喝飲料", [
        ("珍珠奶茶", 60, 2), ("四季春青茶", 35, 1), ("芒果冰沙", 75, 2),
        ("鮮奶綠", 55, 2), ("檸檬多多", 50, 2),
    ]),
]


class CityMap:
    """建立後不可修改。

    - edges：開放路段（無向，u < v），索引即邊 ID
    - base_s：各開放路段的基礎通行秒數，與 edges 同索引
    - adjacency：路口 → [(鄰接路口, 邊 ID)]
    - closed：封閉路段（含被河流截斷的路段）
    - river：河流位置，river[x] = r 表示第 x 行的河道在第 r 與 r+1 列路口之間
    """

    def __init__(self, size: int, seed: int):
        self.size = size
        rng = random.Random(seed)
        self.arterial_rows = _pick_lines(rng, size)
        self.arterial_cols = _pick_lines(rng, size)
        all_edges = _grid_edges(size)
        # 河流用獨立的亂數，不影響其他元素的產生順序
        self.river = _river_rows(random.Random(seed + 1), size)
        river_edges = _river_edges(self.river, size, self._is_arterial)
        self.closed = _close_edges(rng, size, all_edges, self._is_arterial, river_edges)
        closed = set(self.closed)
        self.edges: list[Edge] = [e for e in all_edges if e not in closed]
        self.base_s = [ARTERIAL_S if self._is_arterial(e) else ROAD_S for e in self.edges]
        self.adjacency: dict[Node, list[tuple[Node, int]]] = {
            (x, y): [] for y in range(size) for x in range(size)
        }
        for i, (u, v) in enumerate(self.edges):
            self.adjacency[u].append((v, i))
            self.adjacency[v].append((u, i))
        self.restaurants = _place_restaurants(rng, size)
        self._restaurant_by_id = {r["id"]: r for r in self.restaurants}

    def _is_arterial(self, edge: Edge) -> bool:
        (x1, y1), (x2, y2) = edge
        if y1 == y2:
            return y1 in self.arterial_rows
        return x1 in self.arterial_cols

    def restaurant(self, restaurant_id: str) -> dict | None:
        return self._restaurant_by_id.get(restaurant_id)

    def address_to_node(self, address: str) -> Node:
        digest = hashlib.sha256(address.strip().encode("utf-8")).digest()
        i = int.from_bytes(digest, "big") % (self.size * self.size)
        return (i % self.size, i // self.size)

    def to_map_json(self) -> dict:
        return {
            "size": self.size,
            "base_s": {"road": ROAD_S, "arterial": ARTERIAL_S},
            "closed": [[list(u), list(v)] for u, v in self.closed],
            "arterials": {"rows": list(self.arterial_rows), "cols": list(self.arterial_cols)},
            "river": list(self.river),
            "restaurants": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "node": list(r["node"]),
                    "menu": [dict(item) for item in r["menu"]],
                }
                for r in self.restaurants
            ],
        }


def build_city(size: int, seed: int = CITY_SEED) -> CityMap:
    return CityMap(size, seed)


@lru_cache(maxsize=1)
def get_city() -> CityMap:
    return build_city(config.CITY_GRID_SIZE)


def _grid_edges(size: int) -> list[Edge]:
    edges = []
    for y in range(size):
        for x in range(size):
            if x + 1 < size:
                edges.append(((x, y), (x + 1, y)))
            if y + 1 < size:
                edges.append(((x, y), (x, y + 1)))
    return edges


def _pick_lines(rng: random.Random, size: int) -> list[int]:
    """分層抽樣選出主幹道的位置，讓幾條主幹道大致平均分布。"""
    k = max(1, size // 20)
    band = size / k
    lines = []
    for i in range(k):
        lo = int(i * band + band / 4)
        hi = max(lo, int((i + 1) * band - band / 4) - 1)
        lines.append(rng.randint(lo, min(hi, size - 1)))
    return lines


def _river_rows(rng: random.Random, size: int) -> list[int]:
    """河道沿著列與列之間蜿蜒；相鄰兩行最多差一列，河道不會蓋住任何路口。"""
    if size < 4:
        return []
    base = size * (0.55 + rng.random() * 0.3)
    phase = rng.random() * math.tau
    freq = 7.2 / size  # 整條河約一個多波
    rows = []
    for x in range(size):
        r = round(base + math.sin(x * freq + phase) * size * 0.08)
        if rows:
            r = max(rows[-1] - 1, min(rows[-1] + 1, r))
        rows.append(max(0, min(size - 2, r)))
    return rows


def _river_edges(rows: list[int], size: int, is_arterial) -> list[Edge]:
    """被河流截斷的路段：跨河的直向路段，以及河道轉彎時斜穿過的橫向路段。主幹道是橋，不截斷。"""
    edges = []
    for x, r in enumerate(rows):
        edges.append(((x, r), (x, r + 1)))
        if x + 1 < len(rows) and rows[x + 1] != r:
            m = max(r, rows[x + 1])
            edges.append(((x, m), (x + 1, m)))
    return [e for e in edges if not is_arterial(e)]


def _close_edges(rng, size, all_edges, is_arterial, preclosed=()) -> list[Edge]:
    """先封閉 preclosed（河流），再逐條隨機嘗試移除非主幹道路段，移除後兩端仍互通才真正封閉。

    原圖連通時，移除 (u, v) 後只要 u 仍可到達 v，整張圖就仍連通。
    河流只截斷非主幹道，主幹道橫跨全城，因此封閉河流後整張圖仍連通。
    """
    neighbors: dict[Node, set[Node]] = {(x, y): set() for y in range(size) for x in range(size)}
    for u, v in all_edges:
        neighbors[u].add(v)
        neighbors[v].add(u)
    pre = set(preclosed)
    for u, v in pre:
        neighbors[u].discard(v)
        neighbors[v].discard(u)

    candidates = [e for e in all_edges if not is_arterial(e) and e not in pre]
    rng.shuffle(candidates)
    target = round(len(all_edges) * CLOSED_RATIO)
    closed = list(pre)
    for u, v in candidates:
        if len(closed) >= target:
            break
        neighbors[u].discard(v)
        neighbors[v].discard(u)
        if _reaches(neighbors, u, v):
            closed.append((u, v))
        else:
            neighbors[u].add(v)
            neighbors[v].add(u)
    return sorted(closed)


def _reaches(neighbors: dict[Node, set[Node]], start: Node, goal: Node) -> bool:
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            return True
        for nb in neighbors[node]:
            if nb not in seen:
                seen.add(nb)
                queue.append(nb)
    return False


def _place_restaurants(rng: random.Random, size: int) -> list[dict]:
    margin = size // 10
    span = range(margin, size - margin)
    nodes: list[Node] = []
    while len(nodes) < len(MENUS):
        node = (rng.choice(span), rng.choice(span))
        if node not in nodes:
            nodes.append(node)
    restaurants = []
    for i, ((name, menu), node) in enumerate(zip(MENUS, nodes), start=1):
        rid = f"r{i}"
        restaurants.append({
            "id": rid,
            "name": name,
            "node": node,
            "menu": [
                {"id": f"{rid}-m{j}", "name": item, "price": price, "prep_min": prep}
                for j, (item, price, prep) in enumerate(menu, start=1)
            ],
        })
    return restaurants
