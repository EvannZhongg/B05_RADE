import json
import os
import tempfile
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch

import pytest

from agent_core import JsonlSessionStore, JobManager, LLMResponse, Session, ToolCall, Workspace
from agent_core.execution import CommandExecutionResult
from agent_runtime.experiments import ExperimentRunner
from agent_runtime.scene_input import prepare_scene_input
from rade_tasks.algorithms.base import Strategy, StrategyProfile
from rade_tasks.algorithms.implementations.marginal_greedy import solve
from rade_tasks.algorithms.registry import BUILTIN_STRATEGIES
from rade_tasks.knowledge import KnowledgeService
from rade_tasks.scene import SceneState
from tests.bridge_fixtures import BridgeTestCase, emitted, open_session_message


@pytest.fixture
def strategy(tmp_path, monkeypatch):
    (tmp_path / "test_experiment_solver.py").write_text(
        "from pathlib import Path\n"
        "from time import sleep\n"
        "def solve(scene, parameters, *, seed):\n"
        "    assert scene.P == ((0.8, 0.7), (1, 0)) and scene.v in ((10, 10), (10, 9))\n"
        "    assert seed == 42\n"
        "    mode = parameters['mode']\n"
        "    if mode in ('timeout', 'cancel'):\n"
        "        Path(__file__).with_suffix('.started').touch()\n"
        "        sleep(60)\n"
        "    if mode == 'error':\n"
        "        raise RuntimeError('solver error')\n"
        "    return [{'baseline': 0, 'candidate': 1, 'invalid': True}[mode], 0]\n",
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
    files_path = run_directory / "files"
    files_path.mkdir(parents=True)
    files = Workspace(files_path)
    source = tmp_path / "scene1_1.json"
    source.write_text('{"P":[[0.8,0.7],[1,0]],"v":[10,10]}', encoding="utf-8")
    with store.open_journal("run") as sink:
        session = Session("run")
        session.attach_journal_sink(sink)
        turn_id = session.begin_turn("scene")
        jobs = JobManager(session)
        try:
            runner = ExperimentRunner((strategy,), jobs, files)
            scene = prepare_scene_input(tmp_path, source.name, files)
            yield runner, scene, turn_id, jobs, files, monotonic() + 60
        finally:
            jobs.close()


@pytest.mark.parametrize(
    ("mode", "status", "diagnostic"),
    [("invalid", "invalid", "task index"), ("timeout", "timed_out", "Exceeded"), ("error", "failed", "solver error")],
)
def test_invalid_results_and_process_errors_are_recorded(experiment_run, mode, status, diagnostic):
    runner, scene, turn_id, jobs, files, deadline = experiment_run
    experiment_id = runner.submit("fixture", mode, scene, turn_id, deadline=deadline)["experiment_id"]
    jobs.wait_for_turn(turn_id)
    result = runner.get(experiment_id)
    assert result["status"] == status
    assert result["valid"] is (False if status == "invalid" else None)
    assert diagnostic in result["diagnostic"]
    assert result["elapsed_seconds"] > 0
    assert result["solver_elapsed_seconds"] > 0
    assert result["result_ref"] is None
    assert not (files.path / "experiments").exists()


def test_discovery_and_submission_validate_profiles(experiment_run):
    runner, scene, turn_id, jobs, _, deadline = experiment_run
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
            runner.submit(strategy_id, config_id, scene, turn_id, deadline=deadline)
    assert jobs.drain_updates(turn_id) == ()


def test_can_cancel_a_running_solver(experiment_run, tmp_path):
    runner, scene, turn_id, jobs, files, deadline = experiment_run
    experiment_id = runner.submit("fixture", "cancel", scene, turn_id, deadline=deadline)["experiment_id"]
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
    assert not (files.path / "experiments").exists()


def test_comparison_rejects_different_source_snapshots(experiment_run, tmp_path):
    runner, scene, turn_id, jobs, files, deadline = experiment_run
    baseline = runner.submit("fixture", "baseline", scene, turn_id, deadline=deadline)["experiment_id"]
    source = tmp_path / "scene1_1.json"
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = prepare_scene_input(tmp_path, source.name, files)
    candidate = runner.submit("fixture", "candidate", changed, turn_id, deadline=deadline)["experiment_id"]
    jobs.wait_for_turn(turn_id)
    with pytest.raises(ValueError, match="same scene snapshot"):
        runner.get(candidate, baseline)


def test_marginal_greedy_accounts_for_previous_assignments_and_breaks_ties_by_task_index():
    scene = SceneState('{"P":[[0.5,0.5,1],[0.5,0.5,1],[0,0,1]],"v":[10,10,-1]}').snapshot
    assert solve(scene, {}, seed=0) == [0, 1, None]


def test_builtin_baseline_is_discoverable_and_runs_in_the_worker(experiment_run):
    _, scene, turn_id, jobs, files, deadline = experiment_run
    runner = ExperimentRunner(BUILTIN_STRATEGIES, jobs, files)
    strategy, = runner.registry.discover()
    assert strategy["strategy_id"] == "marginal_greedy"
    profile = strategy["configs"][0]
    assert profile["config_id"] == "default"

    experiment_id = runner.submit(
        strategy["strategy_id"], profile["config_id"], scene, turn_id, deadline=deadline,
    )["experiment_id"]
    jobs.wait_for_turn(turn_id)
    result = runner.get(experiment_id)
    assert result["valid"] is True, result
    assert result["metrics"]["expected_value"] == 10.0
    allocation = json.loads((files.path / result["result_ref"]).read_text(encoding="utf-8"))
    assert allocation == {"allocation": [0, 0]}


def test_solver_uses_remaining_budget_when_its_job_starts(experiment_run):
    runner, scene, turn_id, jobs, _, _ = experiment_run
    start = monotonic()
    with (
        patch("agent_runtime.experiments.monotonic", return_value=start + 4.75),
        patch("agent_runtime.experiments.HostCommandExecutor") as executor,
    ):
        executor.return_value.execute.return_value = CommandExecutionResult(
            "solver", -1, "", "", timed_out=True,
        )
        active = runner.submit("fixture", "cancel", scene, turn_id, deadline=start + 5)
        expired = runner.submit("fixture", "cancel", scene, turn_id, deadline=start + 4)
        jobs.wait_for_turn(turn_id)

    assert executor.return_value.execute.call_count == 1
    assert executor.return_value.execute.call_args.args[1] == 0.25
    assert runner.get(active["experiment_id"])["status"] == "timed_out"
    assert runner.get(expired["experiment_id"])["status"] == "timed_out"


class TestExperimentBridge(BridgeTestCase):
    @pytest.fixture(autouse=True)
    def solver(self, strategy):
        self.strategy = strategy

    def test_scene_tools_submit_evaluate_compare_and_restore(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bridge, stdout = self.make_bridge([], root)
        message = open_session_message(root, agent_config={
            "max_same_tool_calls": 5,
            "output_reserve_tokens": 100,
            "scratch_workspace_root": str(root / "scratch"),
            "main_agent": {"tools": {"read_file": True}},
        })
        bridge.open_session(message)
        inputs = root / "input"
        inputs.mkdir(exist_ok=True)
        source = inputs / "scene1_1.json"
        source.write_text('{"P":[[0.8,0.7],[1,0]],"v":[10,10]}', encoding="utf-8")

        def respond(request, *_):
            names = {tool.name for tool in request.tools}
            self.assertTrue({
                "strategy_list", "experiment_run", "experiment_get", "read_file",
                "knowledge_query", "reflection_propose", "knowledge_explain",
            } <= names)
            results = {
                item.tool_call_id: json.loads(item.content)["output"]
                for item in bridge.host.sessions.session.items if item.role == "tool"
            }
            if "query" not in results:
                return LLMResponse(content=None, tool_calls=(ToolCall("query", "knowledge_query", {}),))
            assert results["query"]["knowledge"] == []
            if "discover" not in results:
                source.unlink()
                return LLMResponse(content=None, tool_calls=(ToolCall("discover", "strategy_list", {}),))
            if "baseline" not in results:
                return LLMResponse(content=None, tool_calls=(ToolCall(
                    "baseline", "experiment_run", {"strategy_id": "fixture", "config_id": "baseline"},
                ),))
            baseline_id = results["baseline"]["experiment_id"]
            jobs = bridge.host.planes.current.jobs
            if "measured-baseline" not in results:
                if jobs.get(baseline_id).status != "completed":
                    return LLMResponse(content="Waiting for baseline")
                return LLMResponse(content=None, tool_calls=(ToolCall(
                    "measured-baseline", "experiment_get", {"experiment_id": baseline_id},
                ),))
            if "candidate" not in results:
                assert results["measured-baseline"]["metrics"]["expected_value"] == 10
                return LLMResponse(content=None, tool_calls=(ToolCall(
                    "candidate", "experiment_run", {"strategy_id": "fixture", "config_id": "candidate"},
                ),))
            if "reflection" in results:
                return LLMResponse(content="Evaluated")
            if "read-allocation" in results:
                return LLMResponse(content=None, tool_calls=(ToolCall("reflection", "reflection_propose", {
                    "hypothesis": "Different assignments could improve expected value.",
                    "conclusion": "The candidate profile improved expected value by 7.",
                    "reference_experiment_id": results["baseline"]["experiment_id"],
                    "experiment_id": results["candidate"]["experiment_id"],
                    "expected_value_effect": "improved",
                }),))
            if "comparison" in results:
                return LLMResponse(content=None, tool_calls=(ToolCall(
                    "read-allocation", "read_file", {"path": results["comparison"]["result_ref"]},
                ),))
            candidate_id = results["candidate"]["experiment_id"]
            if jobs.get(candidate_id).status != "completed":
                return LLMResponse(content="Waiting for candidate")
            return LLMResponse(content=None, tool_calls=(ToolCall("comparison", "experiment_get", {
                "experiment_id": candidate_id,
                "baseline_experiment_id": baseline_id,
            }),))

        with patch("agent_runtime.plane_manager.BUILTIN_STRATEGIES", (*BUILTIN_STRATEGIES, self.strategy)):
            plane = bridge.host.planes.ensure(bridge.host.sessions)
            with (
                patch.object(plane.provider, "count_input_tokens", return_value=1),
                patch.object(plane.provider, "stream_cancellable", side_effect=respond),
            ):
                bridge.run_scene_task({"turn_id": "scene", "scene_input": source.name, "mode": "rethink"})

        events = emitted(stdout)
        assert events[-1]["type"] == "scene_task_completed", events[-1]
        session = bridge.host.sessions.session
        results = {
            item.tool_call_id: json.loads(item.content)["output"]
            for item in session.items if item.role == "tool"
        }
        assert [strategy["strategy_id"] for strategy in results["discover"]["strategies"]] == ["marginal_greedy", "fixture"]
        assert results["candidate"] == {
            "experiment_id": results["candidate"]["experiment_id"], "status": "submitted", "elapsed_seconds": 0.0,
        }
        comparison = results["comparison"]
        assert comparison["valid"] is True
        assert comparison["seed"] == 42
        assert comparison["version"] == "1.2.3"
        assert comparison["metrics"] == {"expected_value": 17.0, "total_value": 20, "assigned_workers": 2}
        assert comparison["comparison"]["baseline_expected_value"] == 10.0
        assert comparison["comparison"]["expected_value_delta"] == 7.0
        result_ref = comparison["result_ref"]
        assert result_ref.startswith("experiments/")
        assert json.loads((plane.files.path / result_ref).read_text(encoding="utf-8")) == {"allocation": [1, 0]}
        assert not (plane.files.path.parent / "experiments").exists()
        assert results["read-allocation"]["path"] == result_ref
        assert '1| {"allocation": [1, 0]}' in results["read-allocation"]["content"]
        result = events[-1]["task"]
        assert result["status"] == "completed"
        assert result["baseline_expected_value"] == 10.0
        assert result["expected_value"] == 17.0
        assert result["experiment_id"] == comparison["experiment_id"]
        reflection = results["reflection"]
        assert reflection["status"] == "verified"
        assert reflection["source"] == {
            "session_id": session.session_id, "task_id": result["task_id"], "filename": source.name,
            "scene_digest": result["scene_digest"],
            "source_path": json.loads(session.items[0].content)["source_path"],
        }
        assert reflection["evidence"][1]["experiment_id"] == result["experiment_id"]
        output_path = root / "output" / "rethink" / source.name
        assert Path(result["output_path"]) == output_path
        assert json.loads(output_path.read_text(encoding="utf-8")) == {"allocation": [1, 0]}

        restored = bridge.host.sessions.store.load(session.session_id)
        assert restored.journal[-1].payload == result
        jobs = JobManager(restored)
        self.addCleanup(jobs.close)
        runner = ExperimentRunner((), jobs, plane.files)
        assert runner.get(comparison["experiment_id"], results["baseline"]["experiment_id"]) == comparison

        second_source = inputs / "scene1_2.json"
        second_source.write_text('{"P":[[0.8,0.7],[1,0]],"v":[10,9]}', encoding="utf-8")
        other_bridge, other_stdout = self.make_bridge([], root)
        other_bridge.open_session(message)
        other_session = other_bridge.host.sessions.session
        assert other_session.session_id != session.session_id

        def reuse_knowledge(_request, *_):
            results = {
                item.tool_call_id: json.loads(item.content)["output"]
                for item in other_session.items if item.role == "tool"
            }
            if "query" not in results:
                return LLMResponse(content=None, tool_calls=(ToolCall("query", "knowledge_query", {}),))
            knowledge, = results["query"]["knowledge"]
            assert knowledge["knowledge_id"] == reflection["knowledge_id"]
            if "explain" not in results:
                return LLMResponse(content=None, tool_calls=(ToolCall(
                    "explain", "knowledge_explain", {"id": knowledge["knowledge_id"], "version": 1},
                ),))
            assert results["explain"]["recent_evidence"] == [reflection]
            if "reuse" not in results:
                profile = knowledge["alternative"]
                return LLMResponse(content=None, tool_calls=(ToolCall("reuse", "experiment_run", {
                    "strategy_id": profile["strategy_id"], "config_id": profile["config_id"],
                }),))
            return LLMResponse(content="Found prior evidence")

        with patch("agent_runtime.plane_manager.BUILTIN_STRATEGIES", (*BUILTIN_STRATEGIES, self.strategy)):
            other_plane = other_bridge.host.planes.ensure(other_bridge.host.sessions)
            with (
                patch.object(other_plane.provider, "count_input_tokens", return_value=1),
                patch.object(other_plane.provider, "stream_cancellable", side_effect=reuse_knowledge),
            ):
                other_bridge.run_scene_task({"turn_id": "reuse", "scene_input": second_source.name})
        terminal = emitted(other_stdout)[-1]
        assert terminal["type"] == "scene_task_completed", terminal
        assert terminal["task"]["expected_value"] == pytest.approx(16.3)
        assert terminal["task"]["experiment_id"] != terminal["task"]["baseline_experiment_id"]


class TestSceneModes(BridgeTestCase):
    def test_norethink_runs_only_the_baseline_without_an_agent_or_knowledge(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bridge, stdout = self.make_bridge([], root)
        bridge.open_session(open_session_message(root))
        with (
            patch.object(bridge.host.planes, "ensure", side_effect=AssertionError("Agent assembly")),
            patch.object(KnowledgeService, "_connect", side_effect=AssertionError("knowledge access")),
        ):
            bridge.run_scene_task({"turn_id": "baseline", "scene_input": "scene1_1.json", "mode": "norethink"})

        terminal = emitted(stdout)[-1]
        assert terminal["type"] == "scene_task_completed", terminal
        assert terminal["usage"] is None
        task = terminal["task"]
        assert task["mode"] == "norethink"
        assert task["experiment_id"] == task["baseline_experiment_id"]
        assert task["expected_value"] == task["baseline_expected_value"]
        output = root / "output" / "norethink" / "scene1_1.json"
        assert Path(task["output_path"]) == output
        assert json.loads(output.read_text(encoding="utf-8")) == {"allocation": [0, 1]}
        assert not (root / "output" / "rethink").exists()
        assert not (root / "knowledge.sqlite3").exists()
        session = bridge.host.sessions.session
        assert len(session.jobs) == 1
        assert not session.turns
        restored = bridge.host.sessions.store.load(session.session_id)
        assert restored.journal[-1].payload == task
        assert json.loads(restored.items[0].content)["filename"] == "scene1_1.json"

    def test_unknown_mode_fails_without_assembling_the_agent(self):
        bridge, stdout = self.make_bridge([])
        bridge.open_session(open_session_message(bridge.host.config_directory))
        with patch.object(bridge.host.planes, "ensure", side_effect=AssertionError("Agent assembly")):
            bridge.run_scene_task({"turn_id": "invalid", "scene_input": "scene1_1.json", "mode": "../rethink"})
        terminal = emitted(stdout)[-1]
        assert terminal["type"] == "scene_task_failed"
        assert terminal["error"]["message"] == "mode must be norethink or rethink"

    def test_queued_baseline_only_task_rejects_steering_and_accepts_cancel(self):
        bridge, stdout = self.make_bridge([])
        bridge.open_session(open_session_message(bridge.host.config_directory))
        bridge.submit({
            "type": "scene_task_start", "turn_id": "baseline",
            "scene_input": "scene1_1.json", "mode": "norethink",
        })
        bridge.submit({"type": "user_steer", "turn_id": "baseline", "steer_id": "s", "text": "try another profile"})
        bridge.submit({"type": "cancel", "turn_id": "baseline"})
        bridge.run_scene_task(bridge.read_message())
        assert emitted(stdout)[-4]["type"] == "scene_task_received"
        assert emitted(stdout)[-3]["type"] == "user_steer_rejected"
        assert emitted(stdout)[-1]["type"] == "scene_task_cancelled"
        assert not bridge.host.sessions.session.jobs
