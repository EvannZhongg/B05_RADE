"""Check allocation heuristics against exact matching and the execution contract."""

import itertools
import math
import random
import subprocess
from importlib import import_module

import pytest

from rade_tasks.algorithms.implementations.global_marginal_greedy import solve as greedy
from rade_tasks.algorithms.implementations.marginal_greedy import solve as row_greedy
from rade_tasks.algorithms.implementations.multi_round_hungarian import (
    maximum_weight_matching as hungarian_matching,
    solve as hungarian,
)
from rade_tasks.algorithms.implementations.multi_round_min_cost_flow import (
    maximum_weight_matching as flow_matching,
    solve as min_cost_flow,
)
from rade_tasks.algorithms.registry import BUILTIN_STRATEGIES, StrategyRegistry
from rade_tasks.evaluation import evaluate
from rade_tasks.execution import execute_strategy
from rade_tasks.scene import SceneSnapshot, SceneState


@pytest.mark.parametrize("matcher", [hungarian_matching, flow_matching])
@pytest.mark.parametrize("rows,columns", list(itertools.product(range(1, 5), repeat=2)))
def test_matching_agrees_with_exhaustive_search(matcher, rows, columns):
    rng = random.Random(42)
    for _ in range(12):
        weights = [[rng.randrange(-10, 21) / 7 for _ in range(columns)] for _ in range(rows)]
        pairs = matcher(weights)
        assert len(pairs) == min(rows, columns)
        assert len({row for row, _ in pairs}) == len(pairs)
        assert len({column for _, column in pairs}) == len(pairs)
        matrix = weights if rows <= columns else list(zip(*weights))
        exact = max(
            math.fsum(matrix[row][column] for row, column in enumerate(permutation))
            for permutation in itertools.permutations(range(len(matrix[0])), len(matrix))
        )
        assert math.fsum(weights[row][column] for row, column in pairs) == pytest.approx(exact)


@pytest.mark.parametrize("matcher", [hungarian_matching, flow_matching])
def test_matching_empty_zero_and_reassignment(matcher):
    assert matcher([]) == []
    assert matcher([[]]) == []
    assert len(matcher([[0, 0], [0, 0]])) == 2
    # The first augmenting path must be undone to reach 17 instead of 10.
    assert matcher([[10, 9], [8, 0]]) == [(0, 1), (1, 0)]


def test_global_greedy_reconsiders_all_workers_and_preserves_ties():
    scene = SceneState('{"P":[[0.8,0.7],[1,0]],"v":[10,10]}').snapshot
    assert row_greedy(scene, {}, seed=0) == [0, 0]
    assert greedy(scene, {}, seed=0) == [1, 0]
    tied = SceneState('{"P":[[0.5,0.5],[0.5,0.5]],"v":[10,10]}').snapshot
    assert greedy(tied, {}, seed=0) == [0, 1]


@pytest.mark.parametrize("solver", [greedy, hungarian, min_cost_flow])
@pytest.mark.parametrize("matrix,values,allocation,expected", [
    ([[0, 0], [0, 0]], [1, 2], [None, None], 0),
    ([[0.5], [0.4], [0.3]], [10], [0, 0, 0], 7.9),
    ([[1], [0.5], [0.3]], [10], [0, None, None], 10),
    ([[0.5, 0.5], [0, 0]], [-10, 0], [None, None], 0),
    ([[1, 0, 1], [0.5, 1, 0.5]], [-10, 0, 10], [2, None], 10),
    ([[0.8, 0.7], [1, 0]], [10, 10], [1, 0], 17),
    ([[0, 0, 0.5]], [3, 5, 7], [2], 3.5),
])
def test_allocation_boundaries(solver, matrix, values, allocation, expected):
    scene = SceneSnapshot(tuple(map(tuple, matrix)), tuple(values))
    assert solver(scene, {}, seed=0) == allocation
    assert evaluate(scene, allocation)["expected_value"] == pytest.approx(expected)


@pytest.mark.parametrize("solver", [greedy, hungarian, min_cost_flow])
def test_solvers_leave_snapshot_intact_and_match_product_objective(solver):
    rng = random.Random(20261116)
    for workers, tasks in [(3, 7), (7, 3), (8, 8)]:
        matrix = tuple(tuple(rng.choice([0, 0.2, 0.5, 1]) for _ in range(tasks))
                       for _ in range(workers))
        values = tuple(rng.randrange(-2, 11) for _ in range(tasks))
        scene = SceneSnapshot(matrix, values)
        allocation = solver(scene, {}, seed=0)
        assert len(allocation) == workers
        assert all(task is None or (0 <= task < tasks and matrix[row][task] > 0
                                   and values[task] > 0)
                   for row, task in enumerate(allocation))
        expected = math.fsum(
            values[task] * (1 - math.prod(1 - matrix[row][task]
                                        for row, selected in enumerate(allocation)
                                        if selected == task))
            for task in range(tasks)
        )
        assert evaluate(scene, allocation)["expected_value"] == pytest.approx(expected)
        assert solver(scene, {}, seed=999) == allocation
        assert scene.P == matrix and scene.v == values


@pytest.mark.parametrize("strategy_id", [
    "marginal_greedy", "global_marginal_greedy",
    "multi_round_hungarian", "multi_round_min_cost_flow",
])
def test_registered_algorithms_run_in_execution_worker(strategy_id):
    registry = StrategyRegistry(BUILTIN_STRATEGIES, max_timeout_seconds=3600)
    discovered = {item["strategy_id"]: item for item in registry.discover()}
    assert "default" in {item["config_id"] for item in discovered[strategy_id]["configs"]}
    strategy, profile = registry.resolve(strategy_id, "default")
    scene = SceneState('{"P":[[0.8,0.7],[1,0]],"v":[10,10]}').snapshot

    def run_process(command, timeout, request):
        result = subprocess.run(command, input=request, capture_output=True, timeout=timeout, check=True)
        return result.stdout.decode("utf-8")

    result = execute_strategy(strategy, profile, scene, run_process)
    assert result.status == "completed", result.diagnostic
    assert result.metrics["expected_value"] == (10 if strategy_id == "marginal_greedy" else 17)
    module, function = strategy.entrypoint.split(":")
    assert result.allocation == getattr(import_module(module), function)(scene, profile.parameters, seed=0)
