"""Choose the largest marginal gain across all remaining worker/task pairs."""

from ...scene import SceneSnapshot


def solve(
    scene: SceneSnapshot, parameters: dict[str, object], *, seed: int,
) -> list[int | None]:
    allocation: list[int | None] = [None] * len(scene.P)
    available = list(range(len(scene.P)))
    failure = [1.0] * len(scene.v)

    while available:
        weighted = [value * q for value, q in zip(scene.v, failure)]
        best_gain = 0.0
        best_worker = best_task = None
        # Strict comparison preserves row, then column order when gains tie.
        for worker in available:
            for task, probability in enumerate(scene.P[worker]):
                gain = weighted[task] * probability
                if gain > best_gain:
                    best_gain, best_worker, best_task = gain, worker, task
        if best_worker is None:
            break
        allocation[best_worker] = best_task
        failure[best_task] *= 1 - scene.P[best_worker][best_task]
        available.remove(best_worker)

    return allocation
