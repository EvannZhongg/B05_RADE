"""Repeat unit-capacity min-cost flow matching using updated marginal gains."""

import heapq
import math
from dataclasses import dataclass

from ...scene import SceneSnapshot
from .round_matching import allocate_rounds


@dataclass(slots=True)
class _Edge:
    target: int
    reverse: int
    capacity: int
    cost: float


def maximum_weight_matching(weights: list[list[float]]) -> list[tuple[int, int]]:
    """Find fixed flow min(rows, columns) with potentials and Dijkstra."""
    if not weights or not weights[0]:
        return []
    transposed = len(weights) > len(weights[0])
    matrix = list(map(list, zip(*weights))) if transposed else weights
    rows, columns = len(matrix), len(matrix[0])
    source, sink = rows + columns, rows + columns + 1
    graph: list[list[_Edge]] = [[] for _ in range(sink + 1)]

    def add_edge(start: int, target: int, cost: float) -> _Edge:
        forward = _Edge(target, len(graph[target]), 1, cost)
        reverse = _Edge(start, len(graph[start]), 0, -cost)
        graph[start].append(forward)
        graph[target].append(reverse)
        return forward

    # A constant shift preserves the fixed-flow optimum and makes initial costs non-negative.
    ceiling = max(max(row) for row in matrix)
    links = []
    for row in range(rows):
        add_edge(source, row, 0.0)
        for column in range(columns):
            link = add_edge(row, rows + column, ceiling - matrix[row][column])
            links.append((row, column, link))
    for column in range(columns):
        add_edge(rows + column, sink, 0.0)

    potential = [0.0] * len(graph)
    for _ in range(rows):
        distance = [math.inf] * len(graph)
        previous: list[tuple[int, int] | None] = [None] * len(graph)
        distance[source] = 0.0
        queue = [(0.0, source)]
        while queue:
            current_distance, node = heapq.heappop(queue)
            if current_distance > distance[node]:
                continue
            for index, edge in enumerate(graph[node]):
                if not edge.capacity:
                    continue
                reduced = edge.cost + potential[node] - potential[edge.target]
                if reduced < -1e-8:
                    raise ArithmeticError("Negative reduced cost in min-cost flow")
                candidate = current_distance + max(0.0, reduced)
                if candidate < distance[edge.target]:
                    distance[edge.target] = candidate
                    previous[edge.target] = (node, index)
                    heapq.heappush(queue, (candidate, edge.target))
        if not math.isfinite(distance[sink]):
            raise ArithmeticError("Fixed matching flow is unreachable")
        for node, dist in enumerate(distance):
            if math.isfinite(dist):
                potential[node] += dist
        node = sink
        while node != source:
            start, index = previous[node]
            edge = graph[start][index]
            edge.capacity -= 1
            graph[node][edge.reverse].capacity += 1
            node = start

    return sorted(
        (column, row) if transposed else (row, column)
        for row, column, edge in links if edge.capacity == 0
    )


def solve(
    scene: SceneSnapshot, parameters: dict[str, object], *, seed: int,
) -> list[int | None]:
    return allocate_rounds(scene, maximum_weight_matching)
