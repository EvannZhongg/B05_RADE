"""Validate allocations and independently recompute their expected value."""

from math import fsum

from .scene import SceneSnapshot


class AllocationValidationError(ValueError):
    def __init__(self, errors: list[dict[str, object]]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} allocation error(s): {errors[0]['message']}")


def evaluate(
    scene: SceneSnapshot, allocation: object, *, detailed: bool = False,
) -> dict[str, object]:
    errors: list[dict[str, object]] = []
    if not isinstance(allocation, list):
        raise AllocationValidationError([{
            "code": "allocation_type", "message": "Allocation must be an array",
        }])
    for worker in range(max(len(scene.P), len(allocation))):
        if worker >= len(allocation):
            errors.append({"code": "missing_assignment", "worker_index": worker,
                           "message": f"Processing unit {worker} has no assignment entry"})
        elif worker >= len(scene.P):
            errors.append({"code": "unexpected_assignment", "worker_index": worker,
                           "message": f"No processing unit exists at row {worker}"})
        elif allocation[worker] is not None and (
            type(allocation[worker]) is not int or not 0 <= allocation[worker] < len(scene.v)
        ):
            errors.append({
                "code": "invalid_task_index", "worker_index": worker,
                "received": repr(allocation[worker]),
                "message": f"Processing unit {worker} must have a task index in [0, {len(scene.v)}) or null",
            })
    if errors:
        raise AllocationValidationError(errors)

    success_probabilities = [0.0] * len(scene.v)
    assigned_workers = 0
    task_workers = [0] * len(scene.v)
    for worker, task in enumerate(allocation):
        if task is None:
            continue
        # Accumulate 1 - product(1 - p) without subtracting nearly equal values.
        success = success_probabilities[task]
        success_probabilities[task] += (1 - success) * scene.P[worker][task]
        assigned_workers += 1
        task_workers[task] += 1

    metrics: dict[str, object] = {
        "expected_value": fsum(
            value * success for value, success in zip(scene.v, success_probabilities)
        ),
        "total_value": sum(scene.v),
        "assigned_workers": assigned_workers,
        "unassigned_workers": len(scene.P) - assigned_workers,
        "covered_tasks": sum(success > 0 for success in success_probabilities),
    }
    if detailed:
        tasks = [{
            "task_index": task, "value": value,
            "assigned_workers": task_workers[task],
            "candidate_workers": sum(row[task] > 0 for row in scene.P),
            "success_probability": success,
            "expected_value": value * success,
        } for task, (value, success) in enumerate(zip(scene.v, success_probabilities))]
        metrics["task_metrics"] = tasks
        metrics["weakest_tasks"] = sorted(
            (task for task in tasks if task["value"] > 0),
            key=lambda task: (task["expected_value"], task["task_index"]),
        )[:5]
    return metrics
