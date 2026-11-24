"""Budgeted relocations and pair swaps, seeded by multi-round Hungarian matching."""

import random
from time import monotonic

from ...evaluation import evaluate
from ...scene import SceneSnapshot
from .multi_round_hungarian import solve as hungarian


def _descent(
    scene: SceneSnapshot, allocation: list[int | None], deadline: float,
) -> list[int | None]:
    matrix, values = scene.P, scene.v
    members: list[set[int]] = [set() for _ in values]
    failure = [1.0] * len(values)
    # residual[i] is v[a] times the failure probability of a's workers except i.
    residual = [0.0] * len(matrix)
    for worker, task in enumerate(allocation):
        if task is not None:
            members[task].add(worker)

    def refresh(task: int) -> None:
        workers = sorted(members[task])
        prefix = [1.0]
        for worker in workers:
            prefix.append(prefix[-1] * (1 - matrix[worker][task]))
        failure[task] = prefix[-1]
        suffix = 1.0
        for index in range(len(workers) - 1, -1, -1):
            worker = workers[index]
            residual[worker] = values[task] * prefix[index] * suffix
            suffix *= 1 - matrix[worker][task]

    for task in range(len(values)):
        refresh(task)
    current_value = float(evaluate(scene, allocation)["expected_value"])
    tolerance = 1e-12 * max(1.0, abs(current_value))

    def accept(changes: dict[int, int | None]) -> bool:
        nonlocal current_value
        candidate = allocation.copy()
        for worker, task in changes.items():
            candidate[worker] = (
                task if task is not None and values[task] > 0 and matrix[worker][task] > 0
                else None
            )
        # Independently verify the whole objective before adopting a delta-based move.
        candidate_value = float(evaluate(scene, candidate)["expected_value"])
        if candidate_value <= current_value + tolerance:
            return False
        affected = set()
        for worker in changes:
            old, new = allocation[worker], candidate[worker]
            if old is not None:
                members[old].remove(worker)
                affected.add(old)
            if new is not None:
                members[new].add(worker)
                affected.add(new)
            allocation[worker] = new
            residual[worker] = 0.0
        for task in affected:
            refresh(task)
        current_value = candidate_value
        return True

    while monotonic() < deadline:
        changed = False
        for worker, row in enumerate(matrix):
            if monotonic() >= deadline:
                return allocation
            source = allocation[worker]
            loss = residual[worker] * row[source] if source is not None else 0.0
            best_gain, target = tolerance, source
            for task, probability in enumerate(row):
                if task == source or values[task] <= 0:
                    continue
                gain = values[task] * failure[task] * probability - loss
                if gain > best_gain:
                    best_gain, target = gain, task
            if target != source:
                changed |= accept({worker: target})

        for first, row in enumerate(matrix):
            if monotonic() >= deadline:
                return allocation
            source = allocation[first]
            best_gain, partner = tolerance, None
            for second in range(first + 1, len(matrix)):
                target = allocation[second]
                if target == source:
                    continue
                gain = 0.0
                if source is not None:
                    gain += residual[first] * (matrix[second][source] - row[source])
                if target is not None:
                    gain += residual[second] * (row[target] - matrix[second][target])
                if gain > best_gain:
                    best_gain, partner = gain, second
            if partner is not None:
                changed |= accept({first: allocation[partner], partner: source})
        if not changed:
            break
    return allocation


def solve(
    scene: SceneSnapshot, parameters: dict[str, object], *, seed: int,
) -> list[int | None]:
    deadline = monotonic() + float(parameters["budget_seconds"])
    best = hungarian(scene, {}, seed=seed)
    best_value = float(evaluate(scene, best)["expected_value"])
    candidate = _descent(scene, best.copy(), deadline)
    rng = random.Random(seed)
    eligible = [
        [task for task, probability in enumerate(row) if probability > 0 and scene.v[task] > 0]
        for row in scene.P
    ]
    active = [worker for worker, tasks in enumerate(eligible) if tasks]
    for restart in range(int(parameters["restarts"]) + 1):
        value = float(evaluate(scene, candidate)["expected_value"])
        if value > best_value:
            best, best_value = candidate.copy(), value
        if restart == parameters["restarts"] or monotonic() >= deadline or not active:
            break
        # A restart may regress; only its verified improvement can replace the incumbent.
        candidate = best.copy()
        for worker in rng.sample(active, min(len(active), max(2, len(active) // 20))):
            candidate[worker] = rng.choice([None, *eligible[worker]])
        candidate = _descent(scene, candidate, deadline)
    return best
