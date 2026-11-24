import itertools
import random
from time import monotonic
from unittest.mock import patch

import pytest

from agent_runtime.config import SceneTaskConfig, load_scene_task_config
from rade_tasks.algorithms.implementations import hungarian_local_search as local
from rade_tasks.algorithms.implementations.multi_round_hungarian import solve as hungarian
from rade_tasks.evaluation import evaluate
from rade_tasks.scene import SceneSnapshot


def value(scene, allocation):
    return evaluate(scene, allocation)["expected_value"]


def test_default_scene_baseline_matches_bundled_config(tmp_path):
    from importlib.resources import files

    path = tmp_path / "agent_config.json"
    path.write_text("{}", encoding="utf-8")
    assert load_scene_task_config(path) == SceneTaskConfig()
    path.write_text(files("agent_runtime.defaults").joinpath("agent_config.json").read_text(encoding="utf-8"),
                    encoding="utf-8")
    assert load_scene_task_config(path) == SceneTaskConfig()
    assert SceneTaskConfig().baseline_strategy_id == "multi_round_hungarian"


@pytest.mark.parametrize("matrix,requires_swap", [
    (((0, .5, .2), (.2, .8, 0), (.5, .2, .8), (.5, .5, 0), (.2, 0, .8)), False),
    (((.8, 1, .5), (.5, .2, .2), (0, .5, 1), (.8, 1, 0), (.5, 1, 1)), True),
])
def test_relocations_and_swaps_escape_hungarian_solution(matrix, requires_swap):
    scene = SceneSnapshot(matrix, (10, 10, 10))
    baseline = hungarian(scene, {}, seed=0)
    if requires_swap:
        for worker, task in itertools.product(range(len(matrix)), [None, 0, 1, 2]):
            candidate = baseline.copy()
            candidate[worker] = task
            assert value(scene, candidate) <= value(scene, baseline)
    result = local.solve(scene, {"budget_seconds": 2, "restarts": 0}, seed=0)
    assert value(scene, result) > value(scene, baseline)


def test_descent_is_monotonic_and_reaches_relocation_swap_local_optimum():
    rng = random.Random(27)
    for _ in range(30):
        scene = SceneSnapshot(
            tuple(tuple(rng.choice([0, .2, .5, 1]) for _ in range(4)) for _ in range(7)),
            (-10, 0, 7, 10),
        )
        start = [rng.choice([None, 2, 3]) for _ in scene.P]
        measured = []

        def record(snapshot, allocation):
            result = evaluate(snapshot, allocation)
            measured.append(result["expected_value"])
            return result

        with patch.object(local, "evaluate", side_effect=record):
            result = local._descent(scene, start.copy(), monotonic() + 2)
        assert measured == sorted(measured)
        final = value(scene, result)
        for worker, task in itertools.product(range(7), [None, 0, 1, 2, 3]):
            candidate = result.copy()
            candidate[worker] = task
            assert value(scene, candidate) <= final + 1e-10
        for first, second in itertools.combinations(range(7), 2):
            candidate = result.copy()
            candidate[first], candidate[second] = candidate[second], candidate[first]
            assert value(scene, candidate) <= final + 1e-10


def test_expired_budget_returns_hungarian_incumbent():
    scene = SceneSnapshot(((.5, .7), (.8, 0), (.3, 1)), (10, 20))
    baseline = hungarian(scene, {}, seed=0)
    with patch.object(local, "monotonic", side_effect=itertools.count()):
        assert local.solve(scene, {"budget_seconds": .1, "restarts": 4}, seed=0) == baseline


def test_seed_controls_restarts_without_changing_snapshot_or_regressing():
    scene = SceneSnapshot(((.2, .8, .5), (.5, .2, .8), (1, .5, .2), (.8, 1, .5)), (10, 20, 30))
    parameters = {"budget_seconds": 2, "restarts": 4}
    perturbations = []
    descent = local._descent

    def record(snapshot, allocation, deadline):
        perturbations.append(allocation.copy())
        return descent(snapshot, allocation, deadline)

    results, starts = [], []
    for seed in (42, 42, 99):
        perturbations.clear()
        with patch.object(local, "_descent", side_effect=record):
            results.append(local.solve(scene, parameters, seed=seed))
        starts.append(perturbations.copy())
    assert starts[0] == starts[1] and results[0] == results[1]
    assert starts[0][1:] != starts[2][1:]
    baseline = value(scene, hungarian(scene, {}, seed=0))
    assert all(value(scene, result) >= baseline for result in results)
    assert scene.P[0] == (.2, .8, .5)
