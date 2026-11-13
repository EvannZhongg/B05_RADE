"""Validate allocations and independently recompute their expected value."""

from math import fsum

from .scene import SceneSnapshot


def evaluate(scene: SceneSnapshot, allocation: object) -> dict[str, int | float]:
    if not isinstance(allocation, list) or len(allocation) != len(scene.P):
        raise ValueError(f"Expected one assignment for each of the {len(scene.P)} processing units")

    success_probabilities = [0.0] * len(scene.v)
    assigned_workers = 0
    for worker, task in enumerate(allocation):
        if task is None:
            continue
        if type(task) is not int or not 0 <= task < len(scene.v):
            raise ValueError(f"Processing unit {worker} must have a task index in [0, {len(scene.v)}) or null")
        # Accumulate 1 - product(1 - p) without subtracting nearly equal values.
        success = success_probabilities[task]
        success_probabilities[task] += (1 - success) * scene.P[worker][task]
        assigned_workers += 1

    return {
        "expected_value": fsum(
            value * success for value, success in zip(scene.v, success_probabilities)
        ),
        "total_value": sum(scene.v),
        "assigned_workers": assigned_workers,
    }
