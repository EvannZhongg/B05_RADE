"""Submit scene experiments to the run's job manager and query their evaluations."""

import json
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import cast
from uuid import uuid4

from agent_core import HostCommandExecutor, JobCancelled, JobContext, JobManager, JobResult, Workspace
from agent_core.atomic import atomic_write_bytes
from agent_core.execution.process import MAX_COMMAND_TIMEOUT_SECONDS, CommandCancelled
from agent_core.tools import JSONValue
from rade_tasks.algorithms.base import Strategy, StrategyProfile
from rade_tasks.algorithms.registry import StrategyRegistry
from rade_tasks.execution import execute_strategy
from rade_tasks.experiments import ExperimentRecord, query_experiment
from rade_tasks.scene import SceneSnapshot

from .scene_input import SceneInput


class ExperimentRunner:
    def __init__(self, strategies: tuple[Strategy, ...], jobs: JobManager, files: Workspace) -> None:
        self.registry = StrategyRegistry(strategies, max_timeout_seconds=MAX_COMMAND_TIMEOUT_SECONDS)
        self._jobs = jobs
        self._files = files

    def submit(
        self, strategy_id: str, config_id: str, scene: SceneInput, turn_id: str, *, deadline: float,
    ) -> dict[str, JSONValue]:
        strategy, profile = self.registry.resolve(strategy_id, config_id)
        snapshot = scene.state.snapshot
        result_ref = f"experiments/{uuid4().hex}/allocation.json"
        handle = self._jobs.submit(
            "experiment", turn_id,
            lambda job: self._execute(job, strategy, profile, snapshot, result_ref, deadline),
            details={
                "strategy_id": strategy_id,
                "config_id": config_id,
                "version": strategy.version,
                "seed": str(profile.seed),
                "timeout_seconds": str(profile.timeout_seconds),
                "scene_digest": scene.digest,
                "task_id": turn_id,
            },
        )
        return {"experiment_id": handle.job_id, "status": handle.status, "elapsed_seconds": 0.0}

    def _execute(
        self, job: JobContext, strategy: Strategy, profile: StrategyProfile, scene: SceneSnapshot,
        result_ref: str, deadline: float,
    ) -> JobResult:
        with TemporaryDirectory(prefix="RADE-experiment-") as directory:
            executor = HostCommandExecutor(Path(directory))
            result = execute_strategy(
                strategy, profile, scene,
                lambda command, timeout, request: _run_process(executor, job, command, timeout, request, deadline),
            )

        job.cancellation.raise_if_cancelled()
        summary: dict[str, JSONValue] = {
            "status": result.status,
            "solver_elapsed_seconds": result.elapsed_seconds,
        }
        if result.status == "completed":
            data = json.dumps({"allocation": result.allocation}, allow_nan=False).encode("utf-8")
            atomic_write_bytes(self._files.resolve_path(result_ref), data)
            summary.update(valid=True, metrics=result.metrics, result_ref=result_ref,
                           allocation_digest=sha256(data).hexdigest())
        elif result.status == "invalid":
            summary["valid"] = False
        if result.diagnostic is not None:
            summary["diagnostic"] = result.diagnostic
        return JobResult(
            output={
                "status": result.status,
                "next": "Use experiment_get with the job ID to inspect the evaluation.",
            },
            result_summary=summary,
        )

    def get(self, experiment_id: str, baseline_experiment_id: str | None = None) -> dict[str, JSONValue]:
        return query_experiment(self._record, experiment_id, baseline_experiment_id)

    def allocations(self, scene: SceneInput, experiment_ids: Iterable[str]) -> Iterable[tuple[str, object]]:
        for experiment_id in experiment_ids:
            record = self._record(experiment_id)
            if record.scene_digest == scene.digest:
                allocation = self._read_allocation(record)
                if allocation is not None:
                    yield experiment_id, allocation

    def evidence(self, experiment_id: str) -> tuple[ExperimentRecord, object]:
        record = self._record(experiment_id)
        return record, self._read_allocation(record)

    def _read_allocation(self, record: ExperimentRecord) -> object:
        if not record.valid or record.result_ref is None:
            return None
        try:
            data = self._files.resolve_path(record.result_ref).read_bytes()
        except (OSError, ValueError):
            return None
        if sha256(data).hexdigest() == record.allocation_digest:
            return json.loads(data)["allocation"]
        return None

    def _record(self, experiment_id: str) -> ExperimentRecord:
        try:
            job = self._jobs.get(experiment_id)
        except ValueError:
            raise ValueError(f"Unknown experiment_id: {experiment_id}") from None
        if job.kind != "experiment":
            raise ValueError(f"Unknown experiment_id: {experiment_id}")

        details = job.details
        summary = (job.result_summary or {}) if job.status == "completed" else {}
        diagnostic = job.error
        if job.status == "cancelled":
            diagnostic = "Experiment was cancelled"
        elif job.status == "interrupted":
            diagnostic = "Experiment was interrupted before completion"
        return ExperimentRecord(
            experiment_id=experiment_id,
            strategy_id=details["strategy_id"],
            config_id=details["config_id"],
            version=details["version"],
            seed=int(details["seed"]),
            timeout_seconds=int(details["timeout_seconds"]),
            scene_digest=details["scene_digest"],
            task_id=details["task_id"],
            status=cast(str, summary.get("status", job.status)),
            elapsed_seconds=job.elapsed_seconds,
            solver_elapsed_seconds=cast(float | None, summary.get("solver_elapsed_seconds")),
            valid=cast(bool | None, summary.get("valid")),
            metrics=cast(dict[str, int | float] | None, summary.get("metrics")),
            diagnostic=cast(str | None, summary.get("diagnostic", diagnostic)),
            result_ref=cast(str | None, summary.get("result_ref")),
            allocation_digest=cast(str | None, summary.get("allocation_digest")),
        )


def _run_process(
    executor: HostCommandExecutor, job: JobContext, command: tuple[str, ...], profile_timeout: int,
    request: bytes, deadline: float,
) -> str:
    timeout = min(profile_timeout, deadline - monotonic())
    if timeout <= 0:
        raise TimeoutError("Scene task budget expired before the solver started")
    try:
        execution = executor.execute(command, timeout, cancellation=job.cancellation, stdin=request)
    except CommandCancelled:
        raise JobCancelled from None
    except OSError as error:
        raise RuntimeError(f"Could not start or read the strategy process: {error}") from error
    try:
        job.cancellation.raise_if_cancelled()
        if execution.timed_out:
            raise TimeoutError(f"Exceeded {timeout:.3g} seconds")
        if execution.exit_code != 0:
            message = execution.stderr.strip()[-2000:]
            raise RuntimeError(f"Strategy exited with code {execution.exit_code}: {message}")
        spool = execution.stdout_spool
        return spool.path.read_text(encoding=spool.encoding) if spool is not None else execution.stdout
    finally:
        for spool in (execution.stdout_spool, execution.stderr_spool):
            if spool is not None:
                spool.cleanup()
