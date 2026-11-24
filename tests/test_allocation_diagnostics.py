import json
import math
from dataclasses import replace

import pytest

from rade_tasks.algorithms.registry import BUILTIN_STRATEGIES
from rade_tasks.evaluation import AllocationValidationError, evaluate
from rade_tasks.execution import execute_strategy
from rade_tasks.experiments import ExperimentRecord, query_experiment
from rade_tasks.scene import SceneSnapshot


SCENE = SceneSnapshot(((.5, 0, 1), (.5, .8, 0), (0, 0, 0), (.2, .4, 0)), (10, 20, 30))


@pytest.mark.parametrize("allocation,expected", [
    ([True, -1, 3, "0"], [(i, "invalid_task_index") for i in range(4)]),
    ([False, 2.0], [(0, "invalid_task_index"), (1, "invalid_task_index"),
                    (2, "missing_assignment"), (3, "missing_assignment")]),
    ([None, None, None, None, 0, 1], [(4, "unexpected_assignment"), (5, "unexpected_assignment")]),
])
def test_all_invalid_rows_are_reported_without_partial_scores(allocation, expected):
    with pytest.raises(AllocationValidationError) as caught:
        evaluate(SCENE, allocation)
    assert [(error["worker_index"], error["code"]) for error in caught.value.errors] == expected
    strategy = BUILTIN_STRATEGIES[0]
    result = execute_strategy(strategy, strategy.profiles["default"], SCENE,
                              lambda *_: json.dumps(allocation))
    assert result.status == "invalid"
    assert result.metrics is None and result.task_metrics is None
    assert result.diagnostic["errors"] == caught.value.errors


@pytest.mark.parametrize("output", ["{", "NaN", "[Infinity]", "{}"])
def test_malformed_and_nonfinite_outputs_have_structured_diagnostics(output):
    strategy = BUILTIN_STRATEGIES[0]
    result = execute_strategy(strategy, strategy.profiles["default"], SCENE, lambda *_: output)
    assert result.status == "invalid"
    assert result.diagnostic["code"] in {"invalid_output", "invalid_allocation"}
    json.dumps(result.diagnostic, allow_nan=False)


def test_coverage_counts_positive_success_and_keeps_zero_probability_assignments_legal():
    result = evaluate(SCENE, [0, 0, 1, None], detailed=True)
    assert result["expected_value"] == 7.5
    assert result["assigned_workers"] == 3
    assert result["unassigned_workers"] == 1
    assert result["covered_tasks"] == 1
    assert [item["task_index"] for item in result["weakest_tasks"]] == [1, 2, 0]
    assert result["weakest_tasks"][0] == {
        "task_index": 1, "value": 20, "assigned_workers": 1, "candidate_workers": 2,
        "success_probability": 0.0, "expected_value": 0.0,
    }
    assert math.fsum(item["expected_value"] for item in result["task_metrics"]) == result["expected_value"]


def test_weakest_tasks_are_top_five_positive_value_tasks_with_unreachable_candidates_visible():
    scene = SceneSnapshot(((0, 0, 0, 0, 0, 0, 0, .1),), (-10, 0, 1, 2, 3, 4, 5, 6))
    metrics = evaluate(scene, [7], detailed=True)
    assert [task["task_index"] for task in metrics["weakest_tasks"]] == [2, 3, 4, 5, 6]
    assert all(task["candidate_workers"] == 0 for task in metrics["weakest_tasks"])


def test_per_task_deltas_show_regressions_and_reconcile_with_total_delta():
    def record(experiment_id, allocation):
        metrics = evaluate(SCENE, allocation, detailed=True)
        tasks = metrics.pop("task_metrics")
        return ExperimentRecord(
            experiment_id=experiment_id, strategy_id="fixture", config_id="default", parameters={},
            version="1", seed=0, timeout_seconds=10, scene_digest="same-scene", task_id="turn",
            status="completed", elapsed_seconds=.1, solver_elapsed_seconds=.1, valid=True,
            metrics=metrics, task_metrics=tasks, diagnostic=None, result_ref="allocation.json",
            allocation_digest="digest",
        )

    records = {"baseline": record("baseline", [0, 1, None, None]),
               "candidate": record("candidate", [2, 0, None, None])}
    assert "task_metrics" not in query_experiment(records.__getitem__, "candidate", None)
    compared = query_experiment(records.__getitem__, "candidate", "baseline")["comparison"]
    assert [task["expected_value_delta"] for task in compared["task_deltas"]] == [0, -16, 30]
    assert math.fsum(task["expected_value_delta"] for task in compared["task_deltas"]) == compared["expected_value_delta"]
    records["candidate"] = replace(records["candidate"], status="invalid", valid=False,
                                   metrics=None, task_metrics=None)
    compared = query_experiment(records.__getitem__, "candidate", "baseline")["comparison"]
    assert compared["expected_value_delta"] is None and compared["task_deltas"] is None
