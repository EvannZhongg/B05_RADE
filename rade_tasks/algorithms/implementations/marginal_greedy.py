"""Allocate each worker where it adds the most expected value."""

from ...scene import SceneSnapshot


def solve(
    scene: SceneSnapshot, parameters: dict[str, object], *, seed: int,
) -> list[int | None]:
    allocation: list[int | None] = [None] * len(scene.P)
    # Each task's remaining value is its value weighted by joint failure probability.
    remaining_values = list(scene.v)

    indices = range(len(scene.P))
    workers = reversed(indices) if parameters.get("reverse_workers") else indices
    for worker in workers:
        row = scene.P[worker]
        best_task = None
        best_gain = 0.0
        for task, probability in enumerate(row):
            gain = probability * remaining_values[task]
            if gain > best_gain:
                best_task, best_gain = task, gain

        allocation[worker] = best_task
        if best_task is not None:
            remaining_values[best_task] *= 1 - row[best_task]

    return allocation
