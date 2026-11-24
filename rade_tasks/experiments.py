"""Experiment records and comparisons over independently evaluated allocations."""

from collections.abc import Callable
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    strategy_id: str
    config_id: str
    parameters: dict[str, object]
    version: str
    seed: int
    timeout_seconds: int
    scene_digest: str
    task_id: str
    status: str
    elapsed_seconds: float | None
    solver_elapsed_seconds: float | None
    valid: bool | None
    metrics: dict[str, object] | None
    task_metrics: list[dict[str, object]] | None
    diagnostic: dict[str, object] | None
    result_ref: str | None
    allocation_digest: str | None


def query_experiment(
    lookup: Callable[[str], ExperimentRecord], experiment_id: str, baseline_experiment_id: str | None,
) -> dict:
    experiment = lookup(experiment_id)
    result = asdict(experiment)
    del result["scene_digest"]
    del result["task_id"]
    del result["allocation_digest"]
    del result["task_metrics"]
    if baseline_experiment_id is None:
        return result

    baseline = lookup(baseline_experiment_id)
    if baseline.scene_digest != experiment.scene_digest:
        raise ValueError("Baseline and experiment must use the same scene snapshot")
    score = experiment.metrics["expected_value"] if experiment.metrics is not None else None
    baseline_score = baseline.metrics["expected_value"] if baseline.metrics is not None else None
    result["comparison"] = {
        "baseline_experiment_id": baseline_experiment_id,
        "baseline_status": baseline.status,
        "baseline_expected_value": baseline_score,
        "expected_value_delta": (
            score - baseline_score if score is not None and baseline_score is not None else None
        ),
        "task_deltas": [
            {
                "task_index": task["task_index"],
                "baseline_expected_value": reference["expected_value"],
                "expected_value": task["expected_value"],
                "expected_value_delta": task["expected_value"] - reference["expected_value"],
                "assigned_workers_delta": task["assigned_workers"] - reference["assigned_workers"],
            }
            for task, reference in zip(experiment.task_metrics, baseline.task_metrics)
        ] if experiment.task_metrics is not None and baseline.task_metrics is not None else None,
    }
    return result
