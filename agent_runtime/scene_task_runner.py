"""Run one reflective scene task through the shared execution plane."""

import json
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Callable
from uuid import uuid4

from agent_core import AgentCancelled, JobManager, Session, TokenUsage, TurnControl, Workspace, runtime_error_info
from agent_core.atomic import atomic_write_bytes
from rade_tasks.algorithms.registry import BUILTIN_STRATEGIES
from rade_tasks.knowledge import KnowledgeService
from rade_tasks.results import select_result

from .callbacks import RuntimeCallbacks
from .execution_plane import ExecutionPlane
from .experiments import ExperimentRunner
from .scene_input import DEFAULT_SCENE_MODE, prepare_scene_input, validate_scene_mode


TASK_BUDGET_SECONDS = 600


def latest_scene_task(session: Session, *, recover: bool = False) -> dict[str, object] | None:
    for event in reversed(session.journal):
        if event.event_type == "application_scene_task":
            task = dict(event.payload)
            if recover and task["status"] == "running":
                task.update(status="interrupted", error=asdict(runtime_error_info(
                    RuntimeError("The scene task was interrupted before it finished"),
                )))
            return task
    return None


@dataclass(frozen=True)
class SceneTaskOutcome:
    task: dict[str, object]
    usage: TokenUsage | None = None
    startup_failed: bool = False
    runtime_warnings: tuple[str, ...] = ()


class SceneTaskRunner:
    def __init__(self, callbacks: RuntimeCallbacks, knowledge: KnowledgeService) -> None:
        self.callbacks = callbacks
        self._knowledge = knowledge
        self.turn_id: str | None = None
        self.task_id: str | None = None
        self.control: TurnControl | None = None
        self._reflection_control: TurnControl | None = None
        self._lock = Lock()
        self._queued: dict[str, object] = {}
        self._pending_steers: dict[str, deque[tuple[str, str]]] = {}
        self._pending_cancels: set[str] = set()
        self._shutdown = False

    def enqueue(self, turn_id: str, mode: object = DEFAULT_SCENE_MODE) -> None:
        with self._lock:
            self._queued[turn_id] = mode

    def steer(self, turn_id: str, steer_id: str, text: str) -> bool:
        with self._lock:
            if self.control is not None and turn_id == self.turn_id:
                return (
                    not self.control.cancelled and self._reflection_control is not None
                    and self._reflection_control.steer(steer_id, text)
                )
            if self._queued.get(turn_id) == "rethink" and not self._shutdown:
                self._pending_steers.setdefault(turn_id, deque()).append((steer_id, text))
                return True
            return False

    def cancel(self, turn_id: str) -> bool:
        with self._lock:
            if self.control is not None and turn_id == self.turn_id:
                return self.control.cancel()
            if turn_id in self._queued:
                self._pending_cancels.add(turn_id)
            return False

    def shutdown(self) -> bool:
        with self._lock:
            self._shutdown = True
            return self.control.cancel() if self.control is not None else False

    def run(
        self,
        turn_id: str,
        filename: object,
        input_directory: Path,
        output_directory: Path,
        session: Session,
        files: Workspace,
        ensure_plane: Callable[[TurnControl], ExecutionPlane],
        *,
        mode: object = DEFAULT_SCENE_MODE,
    ) -> SceneTaskOutcome:
        started_at = monotonic()
        deadline = started_at + TASK_BUDGET_SECONDS
        budget_exhausted = False

        def deadline_reached() -> bool:
            nonlocal budget_exhausted
            budget_exhausted = monotonic() >= deadline
            return budget_exhausted

        control = TurnControl(deadline_reached)
        reflection_control = TurnControl(lambda: control.cancelled) if mode == "rethink" else None
        task_id = uuid4().hex
        with self._lock:
            if self.turn_id is not None:
                raise RuntimeError("a scene task is already running")
            self.turn_id = turn_id
            self.task_id = task_id
            self.control = control
            self._reflection_control = reflection_control
            self._queued.pop(turn_id, None)
            steers = self._pending_steers.pop(turn_id, ())
            if reflection_control is not None:
                for steer_id, steer_text in steers:
                    reflection_control.steer(steer_id, steer_text)
            if turn_id in self._pending_cancels or self._shutdown:
                control.cancel()
            self._pending_cancels.discard(turn_id)
        task: dict[str, object] = {
            "task_id": task_id,
            "filename": filename if isinstance(filename, str) else "",
            "mode": mode if mode in ("rethink", "norethink") else DEFAULT_SCENE_MODE,
            "status": "running",
        }
        plane = None
        jobs = None
        memory = None
        assembling = False
        startup_failed = False
        usage = None
        warnings: list[str] = []
        try:
            session.record_application_event("scene_task", task.copy())
            control.raise_if_cancelled()
            mode = validate_scene_mode(mode)
            if not isinstance(filename, str):
                raise ValueError("scene_task_start requires a scene filename")
            scene = prepare_scene_input(input_directory, filename, files)
            task["scene_digest"] = scene.digest
            prompt = json.loads(scene.text)
            prompt["mode"] = mode
            if mode == "norethink":
                jobs = JobManager(session)
                jobs.set_update_callback(self.callbacks.on_job_update)
                experiments = ExperimentRunner(BUILTIN_STRATEGIES, jobs, files)
                session.add_item("user", json.dumps(prompt, ensure_ascii=False, indent=2),
                                 timestamp_utc=datetime.now(timezone.utc))
                self.callbacks.on_phase("running", ())
            else:
                assembling = True
                plane = ensure_plane(control)
                assembling = False
                assert plane.agent is not None and plane.tool_context is not None and plane.jobs is not None
                jobs = plane.jobs
                experiments = plane.tool_context.experiments
                assert experiments is not None
                self.callbacks.on_phase("running", plane.runtime_warnings)
                plane.runtime_warnings = ()
            control.raise_if_cancelled()
            submission = experiments.submit("marginal_greedy", "default", scene, task_id)
            baseline_id = submission["experiment_id"]
            assert isinstance(baseline_id, str)
            while jobs.get(baseline_id).status in {"submitted", "running"}:
                control.raise_if_cancelled()
                jobs.wait_for_update(task_id)
            jobs.drain_updates(task_id)
            control.raise_if_cancelled()
            baseline = experiments.get(baseline_id)
            if baseline["status"] == "timed_out":
                raise TimeoutError(str(baseline["diagnostic"]))
            baseline_metrics = baseline["metrics"]
            if not baseline["valid"] or not isinstance(baseline_metrics, dict):
                raise ValueError(f"Baseline solver failed: {baseline['diagnostic'] or baseline['status']}")
            candidate_ids = [baseline_id]
            if plane is not None:
                assert plane.tool_context is not None and plane.agent is not None
                assert reflection_control is not None
                prompt.update(
                    baseline=baseline,
                    objective=(
                        "Use knowledge_query to look for relevant experience. Reflect on the baseline and "
                        "use registered experiments to test improvements. Compare completed experiments with "
                        "experiment_get, then submit supported findings with reflection_propose. The runtime "
                        "will validate and export the best allocation after this turn."
                    ),
                )
                memory = plane.memory
                if memory is not None:
                    memory.begin_turn()
                tools = plane.select_main_tools(replace(plane.tool_context, scene=scene, knowledge=self._knowledge))
                reflection = plane.agent.run(
                    json.dumps(prompt, ensure_ascii=False, indent=2),
                    turn_id=task_id,
                    turn_control=reflection_control,
                    on_event=lambda event: self.callbacks.on_event(event, task_id),
                    tools=tools,
                )
                usage = reflection.usage
                control.raise_if_cancelled()
                candidate_ids.extend(
                    job.job_id for job in session.jobs.values()
                    if job.kind == "experiment" and job.turn_id == task_id and job.job_id != baseline_id
                )
            result = select_result(scene.state.snapshot, experiments.allocations(scene, candidate_ids))
            output_path = output_directory / mode / filename
            data = result.to_json()
            # Commit is the point after which cancellation can no longer win.
            control.finish()
            control.raise_if_cancelled()
            atomic_write_bytes(output_path, data)
            task.update(
                status="completed", output_path=str(output_path),
                expected_value=result.expected_value, experiment_id=result.experiment_id,
                baseline_experiment_id=baseline_id, baseline_expected_value=baseline_metrics["expected_value"],
            )
            task["elapsed_seconds"] = monotonic() - started_at
            session.record_application_event("scene_task", task.copy())
        except (KeyboardInterrupt, AgentCancelled):
            if budget_exhausted:
                task.update(status="timed_out", error=asdict(runtime_error_info(
                    TimeoutError(f"Scene task exceeded {TASK_BUDGET_SECONDS} seconds"),
                )))
            else:
                task.update(status="cancelled", error=None)
        except Exception as error:
            startup_failed = assembling
            task.pop("output_path", None)
            task.update(status="timed_out" if isinstance(error, TimeoutError) else "failed",
                        error=asdict(runtime_error_info(error)))
        finally:
            try:
                if task["status"] != "completed":
                    if jobs is not None:
                        jobs.cancel_and_discard(task_id)
                    if memory is not None:
                        memory.discard_pending()
                    task["elapsed_seconds"] = monotonic() - started_at
                    try:
                        session.record_application_event("scene_task", task.copy())
                    except (OSError, ValueError) as error:
                        warnings.append(f"Could not persist scene task status: {error}")
                elif memory is not None:
                    try:
                        memory.reconcile_pending()
                    except Exception as error:
                        warnings.append(f"Long-term memory update failed: {error}")
            finally:
                try:
                    if mode == "norethink" and jobs is not None:
                        jobs.close()
                finally:
                    with self._lock:
                        self.control = None
                        self._reflection_control = None
                        self.turn_id = None
                        self.task_id = None
        return SceneTaskOutcome(task, usage, startup_failed, tuple(warnings))
