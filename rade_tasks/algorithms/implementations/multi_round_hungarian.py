"""Maximum-weight Hungarian matching, repeated using updated marginal gains."""

import math

from ...scene import SceneSnapshot
from .round_matching import allocate_rounds


def maximum_weight_matching(weights: list[list[float]]) -> list[tuple[int, int]]:
    """Match min(rows, columns) pairs in a finite rectangular weight matrix."""
    if not weights or not weights[0]:
        return []
    transposed = len(weights) > len(weights[0])
    matrix = list(map(list, zip(*weights))) if transposed else weights
    rows, columns = len(matrix), len(matrix[0])
    row_potential = [0.0] * (rows + 1)
    column_potential = [0.0] * (columns + 1)
    matched = [0] * (columns + 1)
    predecessor = [0] * (columns + 1)

    for row in range(1, rows + 1):
        matched[0] = row
        column = 0
        minimum = [math.inf] * (columns + 1)
        used = [False] * (columns + 1)
        while True:
            used[column] = True
            current_row = matched[column]
            delta, next_column = math.inf, 0
            for candidate in range(1, columns + 1):
                if used[candidate]:
                    continue
                cost = (
                    -matrix[current_row - 1][candidate - 1]
                    - row_potential[current_row] - column_potential[candidate]
                )
                if cost < minimum[candidate]:
                    minimum[candidate] = cost
                    predecessor[candidate] = column
                if minimum[candidate] < delta:
                    delta, next_column = minimum[candidate], candidate
            for candidate in range(columns + 1):
                if used[candidate]:
                    row_potential[matched[candidate]] += delta
                    column_potential[candidate] -= delta
                else:
                    minimum[candidate] -= delta
            column = next_column
            if matched[column] == 0:
                break
        while column:
            previous = predecessor[column]
            matched[column] = matched[previous]
            column = previous

    pairs = [(matched[column] - 1, column - 1)
             for column in range(1, columns + 1) if matched[column]]
    return sorted((column, row) if transposed else (row, column) for row, column in pairs)


def solve(
    scene: SceneSnapshot, parameters: dict[str, object], *, seed: int,
) -> list[int | None]:
    return allocate_rounds(scene, maximum_weight_matching)
