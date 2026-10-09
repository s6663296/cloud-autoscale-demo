"""最短路徑與替代路線（README 4.2），以 heapq 自行實作 Dijkstra。

adjacency：路口 → [(鄰接路口, 邊 ID)]；weights：以邊 ID 為索引的通行秒數。
"""

import heapq
from dataclasses import dataclass

from shared.citymap import Node

Adjacency = dict[Node, list[tuple[Node, int]]]


@dataclass
class Route:
    nodes: list[Node]
    edges: list[int]
    cost: float


def dijkstra_all(adjacency: Adjacency, weights: list[float], source: Node):
    """完整搜尋：回傳 (dist, prev)，prev[node] = (前一個路口, 邊 ID)。"""
    return _search(adjacency, weights, source, None)


def dijkstra_path(adjacency: Adjacency, weights: list[float], source: Node, target: Node) -> Route:
    """點對點搜尋：目標出列即停止。路網保證連通，必定有解。"""
    dist, prev = _search(adjacency, weights, source, target)
    nodes, edges = [target], []
    node = target
    while node != source:
        node, e = prev[node]
        nodes.append(node)
        edges.append(e)
    nodes.reverse()
    edges.reverse()
    return Route(nodes, edges, dist[target])


def _search(adjacency: Adjacency, weights: list[float], source: Node, target: Node | None):
    dist = {source: 0.0}
    prev: dict[Node, tuple[Node, int]] = {}
    done = set()
    heap = [(0.0, source)]
    while heap:
        d, node = heapq.heappop(heap)
        if node in done:
            continue
        if node == target:
            break
        done.add(node)
        for nb, e in adjacency[node]:
            nd = d + weights[e]
            if nd < dist.get(nb, float("inf")):
                dist[nb] = nd
                prev[nb] = (node, e)
                heapq.heappush(heap, (nd, nb))
    return dist, prev


def alternative_routes(
    adjacency: Adjacency, weights: list[float], source: Node, target: Node, k: int, penalty: float
) -> list[Route]:
    """懲罰法：每找到一條路線，就把它的邊在權重副本上乘以 penalty 再重搜。

    回傳 k 條路線，cost 以原始權重計算。
    """
    penalized = list(weights)
    routes = []
    for _ in range(k):
        route = dijkstra_path(adjacency, penalized, source, target)
        route.cost = sum(weights[e] for e in route.edges)
        routes.append(route)
        for e in route.edges:
            penalized[e] *= penalty
    return routes
