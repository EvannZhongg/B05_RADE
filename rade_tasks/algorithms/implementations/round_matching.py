"""Apply a one-to-one matcher to successive rounds of marginal gains."""

from collections.abc import Callable

from ...scene import SceneSnapshot


def allocate_rounds(
    scene: SceneSnapshot,
    matcher: Callable[[list[list[float]]], list[tuple[int, int]]],
) -> list[int | None]:
    allocation: list[int | None] = [None] * len(scene.P)
    available = list(range(len(scene.P)))
    failure = [1.0] * len(scene.v)
    while available:
        # Zero weights let the fixed-cardinality matchers skip non-positive value.
        weights = [
            [max(0.0, value) * failure[task] * probability
             for task, (value, probability) in enumerate(zip(scene.v, scene.P[worker]))]
            for worker in available
        ]
        selected = set()
        for row, task in matcher(weights):
            if weights[row][task] <= 0:
                continue
            worker = available[row]
            allocation[worker] = task
            failure[task] *= 1 - scene.P[worker][task]
            selected.add(worker)
        if not selected:
            break
        available = [worker for worker in available if worker not in selected]
    return allocation
