import json
import os
import tempfile
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch

import pytest

from agent_core import JsonlSessionStore, JobManager, LLMResponse, Session, ToolCall, Workspace
from agent_runtime.experiments import ExperimentRunner
from agent_runtime.scene_input import prepare_scene_input
from rade_tasks.algorithms.base import Strategy, StrategyProfile
from rade_tasks.algorithms.implementations.marginal_greedy import solve
from rade_tasks.algorithms.registry import BUILTIN_STRATEGIES
from rade_tasks.scene import SceneState
from tests.bridge_fixtures import BridgeTestCase, emitted, open_session_message


@pytest.fixture
def strategy(tmp_path, monkeypatch):
    (tmp_path / "test_experiment_solver.py").write_text(
        "from pathlib import Path\n"
        "from time import sleep\n"
        "def solve(scene, parameters, *, seed):\n"
        "    assert scene.P == ((0.5, 0), (0.5, 1)) and scene.v == (10, 3)\n"
        "    assert seed == 42\n"
        "    mode = parameters['mode']\n"
        "    if mode in ('timeout', 'cancel'):\n"
        "        Path(__file__).with_suffix('.started').touch()\n"
        "        sleep(60)\n"
        "    if mode == 'error':\n"
        "        raise RuntimeError('solver error')\n"
        "    return [0, {'baseline': 0, 'candidate': 1, 'invalid': True}[mode]]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path), prepend=os.pathsep)
    return Strategy(
        strategy_id="fixture",
        version="1.2.3",
        description="Test the experiment lifecycle",
        entrypoint="test_experiment_solver:solve",
        parameter_schema={
            "type": "object",
            "properties": {"mode": {"enum": ["baseline", "candidate", "invalid", "timeout", "cancel", "error"]}},
            "required": ["mode"],
            "additionalProperties": False,
        },
        profiles={
            mode: StrategyProfile({"mode": mode}, seed=42, timeout_seconds=1 if mode == "timeout" else 10)
            for mode in ("baseline", "candidate", "invalid", "timeout", "cancel", "error", "bad_profile")
        },
    )


@pytest.fixture
def experiment_run(tmp_path, strategy):
    store = JsonlSessionStore(tmp_path / "runs")
    run_directory = store.session_directory("run")
    files = run_directory / "files"
    files.mkdir(parents=True)
    source = tmp_path / "scene1_1.json"
    source.write_text('{"P":[[0.5,0],[0.5,1]],"v":[10,3]}', encoding="utf-8")
    with store.open_journal("run") as sink:
        session = Session("run")
        session.attach_journal_sink(sink)
        turn_id = session.begin_turn("scene")
        jobs = JobManager(session)
        try:
            runner = ExperimentRunner((strategy,), jobs, run_directory)
            scene = prepare_scene_input(tmp_path, source.name, Workspace(files))
            yield runner, scene, turn_id, jobs, run_directory
        finally:
            jobs.close()


@pytest.mark.parametrize(
    ("mode", "status", "diagnostic"),
    [("invalid", "invalid", "task index"), ("timeout", "timed_out", "Exceeded"), ("error", "failed", "solver error")],
)
def test_invalid_results_and_process_errors_are_recorded(experiment_run, mode, status, diagnostic):
    runner, scene, turn_id, jobs, run_directory = experiment_run
    experiment_id = runner.submit("fixture", mode, scene, turn_id)["experiment_id"]
    jobs.wait_for_turn(turn_id)
    result = runner.get(experiment_id)
    assert result["status"] == status
    assert result["valid"] is (False if status == "invalid" else None)
    assert diagnostic in result["diagnostic"]
    assert result["elapsed_seconds"] > 0
    assert result["solver_elapsed_seconds"] > 0
    assert result["result_ref"] is None
    assert not (run_directory / "experiments").exists()


def test_discovery_and_submission_validate_profiles(experiment_run):
    runner, scene, turn_id, jobs, _ = experiment_run
    strategy, = runner.registry.discover()
    assert strategy["version"] == "1.2.3"
    assert "enum" in strategy["parameter_schema"]["properties"]["mode"]
    assert {config["config_id"] for config in strategy["configs"]} == {
        "baseline", "candidate", "invalid", "timeout", "cancel", "error",
    }
    for strategy_id, config_id, error in (
        ("missing", "baseline", "Unknown strategy_id"),
        ("fixture", "missing", "Unknown config_id"),
        ("fixture", "bad_profile", "Invalid profile"),
    ):
        with pytest.raises(ValueError, match=error):
            runner.submit(strategy_id, config_id, scene, turn_id)
    assert jobs.drain_updates(turn_id) == ()


def test_can_cancel_a_running_solver(experiment_run, tmp_path):
    runner, scene, turn_id, jobs, run_directory = experiment_run
    experiment_id = runner.submit("fixture", "cancel", scene, turn_id)["experiment_id"]
    started = tmp_path / "test_experiment_solver.started"
    deadline = monotonic() + 5
    while not started.is_file() and monotonic() < deadline:
        sleep(0.01)
    assert started.is_file()
    running = runner.get(experiment_id)
    assert running["status"] == "running"
    assert running["valid"] is None
    assert running["result_ref"] is None
    jobs.cancel_and_discard(turn_id)
    result = runner.get(experiment_id)
    assert result["status"] == "cancelled"
    assert result["result_ref"] is None
    assert not (run_directory / "experiments").exists()


def test_comparison_rejects_different_source_snapshots(experiment_run, tmp_path):
    runner, scene, turn_id, jobs, run_directory = experiment_run
    baseline = runner.submit("fixture", "baseline", scene, turn_id)["experiment_id"]
    source = tmp_path / "scene1_1.json"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = prepare_scene_input(tmp_path, source.name, Workspace(run_directory / "files"))
    candidate = runner.submit("fixture", "candidate", changed, turn_id)["experiment_id"]
    jobs.wait_for_turn(turn_id)
    with pytest.raises(ValueError, match="same scene snapshot"):
        runner.get(candidate, baseline)


def test_marginal_greedy_accounts_for_previous_assignments_and_breaks_ties_by_task_index():
    scene = SceneState('{"P":[[0.5,0.5,1],[0.5,0.5,1],[0,0,1]],"v":[10,10,-1]}').snapshot
    assert solve(scene, {}, seed=0) == [0, 1, None]


def test_builtin_baseline_is_discoverable_and_runs_in_the_worker(experiment_run):
    _, scene, turn_id, jobs, run_directory = experiment_run
    runner = ExperimentRunner(BUILTIN_STRATEGIES, jobs, run_directory)
    strategy, = runner.registry.discover()
    assert strategy["strategy_id"] == "marginal_greedy"
    profile, = strategy["configs"]
    assert profile["config_id"] == "default"

    experiment_id = runner.submit(strategy["strategy_id"], profile["config_id"], scene, turn_id)["experiment_id"]
    jobs.wait_for_turn(turn_id)
    result = runner.get(experiment_id)
    assert result["valid"] is True, result
    assert result["metrics"]["expected_value"] == 8.0
    allocation = json.loads((run_directory / result["result_ref"]).read_text(encoding="utf-8"))
    assert allocation == {"allocation": [0, 1]}


class TestExperimentBridge(BridgeTestCase):
    @pytest.fixture(autouse=True)
    def solver(self, strategy):
        self.strategy = strategy

    def test_scene_tools_submit_evaluate_compare_and_restore(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bridge, stdout = self.make_bridge([], root)
        message = open_session_message(root)
        bridge.open_session(message)
        inputs = root / "input"
        inputs.mkdir()
        source = inputs / "scene1_1.json"
        source.write_text('{"P":[[0.5,0],[0.5,1]],"v":[10,3]}', encoding="utf-8")

        def respond(request, *_):
            names = {tool.name for tool in request.tools}
            self.assertTrue({"strategy_list", "experiment_run", "experiment_get"} <= names)
            results = {
                item.tool_call_id: json.loads(item.content)["output"]
                for item in bridge.host.sessions.session.items if item.role == "tool"
            }
            if "discover" not in results:
                source.unlink()
                return LLMResponse(content=None, tool_calls=(ToolCall("discover", "strategy_list", {}),))
            if "candidate" not in results:
                return LLMResponse(content=None, tool_calls=tuple(
                    ToolCall(mode, "experiment_run", {"strategy_id": "fixture", "config_id": mode})
                    for mode in ("baseline", "candidate")
                ))
            if "comparison" in results:
                return LLMResponse(content="Evaluated")
            candidate_id = results["candidate"]["experiment_id"]
            baseline_id = results["baseline"]["experiment_id"]
            jobs = bridge.host.planes.current.jobs
            if any(jobs.get(job_id).status != "completed" for job_id in (candidate_id, baseline_id)):
                return LLMResponse(content="Waiting for experiments")
            return LLMResponse(content=None, tool_calls=(ToolCall("comparison", "experiment_get", {
                "experiment_id": candidate_id,
                "baseline_experiment_id": baseline_id,
            }),))

        with patch("agent_runtime.plane_manager.BUILTIN_STRATEGIES", (self.strategy,)):
            plane = bridge.host.planes.ensure(bridge.host.sessions)
            with (
                patch.object(plane.provider, "count_input_tokens", return_value=1),
                patch.object(plane.provider, "stream_cancellable", side_effect=respond),
            ):
                bridge.run_turn({"turn_id": "scene", "scene_input": source.name})

        events = emitted(stdout)
        assert events[-1]["type"] == "turn_completed", events[-1]
        session = bridge.host.sessions.session
        run_directory = plane.files.path.parent
        results = {
            item.tool_call_id: json.loads(item.content)["output"]
            for item in session.items if item.role == "tool"
        }
        assert results["discover"]["strategies"][0]["strategy_id"] == "fixture"
        assert results["candidate"] == {
            "experiment_id": results["candidate"]["experiment_id"], "status": "submitted", "elapsed_seconds": 0.0,
        }
        comparison = results["comparison"]
        assert comparison["valid"] is True
        assert comparison["seed"] == 42
        assert comparison["version"] == "1.2.3"
        assert comparison["metrics"] == {"expected_value": 8.0, "total_value": 13, "assigned_workers": 2}
        assert comparison["comparison"]["baseline_expected_value"] == 7.5
        assert comparison["comparison"]["expected_value_delta"] == 0.5
        assert json.loads((run_directory / comparison["result_ref"]).read_text()) == {"allocation": [0, 1]}
        assert not (root / "output").exists()

        restored = bridge.host.sessions.store.load(session.session_id)
        jobs = JobManager(restored)
        self.addCleanup(jobs.close)
        runner = ExperimentRunner((), jobs, run_directory)
        assert runner.get(comparison["experiment_id"], results["baseline"]["experiment_id"]) == comparison
