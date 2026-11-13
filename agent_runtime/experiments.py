"""Submit scene experiments to the run's job manager and query their evaluations."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from agent_core import HostCommandExecutor, JobCancelled, JobContext, JobManager, JobResult
from agent_core.atomic import atomic_write_text
from agent_core.execution.process import MAX_COMMAND_TIMEOUT_SECONDS, CommandCancelled
from agent_core.tools import JSONValue
from rade_tasks.evaluation import evaluate
from rade_tasks.scene import SceneSnapshot
from rade_tasks.strategy import Strategy, StrategyProfile

from .scene_input import SceneInput


class ExperimentRunner:
    def __init__(self, strategies: tuple[Strategy, ...], jobs: JobManager, run_directory: Path):
        self._strategies: dict[str, Strategy] = {}
        self._jobs = jobs
        self._run_directory = run_directory
        for strategy in strategies:
            strategy_id = strategy.strategy_id
            if not strategy_id or strategy_id in self._strategies:
                raise ValueError(f"Invalid or duplicate strategy_id: {strategy_id!r}")
            Draft202012Validator.check_schema(strategy.parameter_schema)
            self._strategies[strategy_id] = strategy

    def list_strategies(self) -> list[JSONValue]:
        strategies: list[JSONValue] = []
        for strategy in self._strategies.values():
            configs: list[JSONValue] = []
            for config_id, profile in strategy.profiles.items():
                try:
                    _validate_profile(strategy, profile)
                except ValueError:
                    continue
                configs.append({
                    "config_id": config_id,
                    "parameters": profile.parameters,
                    "seed": profile.seed,
                    "timeout_seconds": profile.timeout_seconds,
                })
            strategies.append({
                "strategy_id": strategy.strategy_id,
                "version": strategy.version,
                "description": strategy.description,
                "parameter_schema": strategy.parameter_schema,
                "configs": configs,
            })
        return strategies

    def submit(self, strategy_id: str, config_id: str, scene: SceneInput, turn_id: str) -> dict[str, JSONValue]:
        strategy = self._strategies.get(strategy_id)
        if strategy is None:
            raise ValueError(f"Unknown strategy_id: {strategy_id}")
        profile = strategy.profiles.get(config_id)
        if profile is None:
            raise ValueError(f"Unknown config_id for strategy {strategy_id}: {config_id}")

        try:
            _validate_profile(strategy, profile)
        except ValueError as error:
            raise ValueError(f"Invalid profile {strategy_id}/{config_id}: {error}") from error

        snapshot = scene.state.snapshot
        request = json.dumps({
            "P": snapshot.P,
            "v": snapshot.v,
            "parameters": profile.parameters,
            "seed": profile.seed,
        }, allow_nan=False).encode("utf-8")
        result_ref = f"experiments/{uuid4().hex}/allocation.json"
        handle = self._jobs.submit(
            "experiment", turn_id,
            lambda job: self._execute(job, strategy, profile, snapshot, request, result_ref),
            details={
                "strategy_id": strategy_id,
                "config_id": config_id,
                "version": strategy.version,
                "seed": str(profile.seed),
                "timeout_seconds": str(profile.timeout_seconds),
                "scene_digest": scene.digest,
            },
        )
        return {"experiment_id": handle.job_id, "status": handle.status, "elapsed_seconds": 0.0}

    def _execute(
        self, job: JobContext, strategy: Strategy, profile: StrategyProfile, scene: SceneSnapshot,
        request: bytes, result_ref: str,
    ) -> JobResult:
        with TemporaryDirectory(prefix="RADE-experiment-") as directory:
            started_at = monotonic()
            try:
                execution = HostCommandExecutor(Path(directory)).execute(
                    (sys.executable, "-P", "-m", "rade_tasks.strategy_worker", strategy.entrypoint),
                    profile.timeout_seconds,
                    cancellation=job.cancellation,
                    stdin=request,
                )
            except CommandCancelled:
                raise JobCancelled from None
            elapsed = monotonic() - started_at
            try:
                job.cancellation.raise_if_cancelled()
                if execution.timed_out:
                    return _result("timed_out", elapsed, diagnostic=f"Exceeded {profile.timeout_seconds} seconds")
                if execution.exit_code != 0:
                    message = execution.stderr.strip()[-2000:]
                    diagnostic = f"Strategy exited with code {execution.exit_code}: {message}"
                    return _result("failed", elapsed, diagnostic=diagnostic)
                output = (
                    execution.stdout_spool.path.read_text(encoding=execution.stdout_spool.encoding)
                    if execution.stdout_spool is not None else execution.stdout
                )
            finally:
                for spool in (execution.stdout_spool, execution.stderr_spool):
                    if spool is not None:
                        spool.cleanup()

        try:
            allocation = json.loads(output)
            metrics = evaluate(scene, allocation)
        except (ValueError, OverflowError) as error:
            return _result("invalid", elapsed, valid=False, diagnostic=str(error))

        job.cancellation.raise_if_cancelled()
        atomic_write_text(self._run_directory / result_ref, json.dumps({"allocation": allocation}))
        return _result("completed", elapsed, valid=True, metrics=metrics, result_ref=result_ref)

    def get(self, experiment_id: str, baseline_experiment_id: str | None = None) -> dict[str, JSONValue]:
        result, scene_digest = self._get(experiment_id)
        if baseline_experiment_id is not None:
            baseline, baseline_scene_digest = self._get(baseline_experiment_id)
            if baseline_scene_digest != scene_digest:
                raise ValueError("Baseline and experiment must use the same scene snapshot")
            metrics = result["metrics"]
            baseline_metrics = baseline["metrics"]
            score = metrics["expected_value"] if isinstance(metrics, dict) else None
            baseline_score = baseline_metrics["expected_value"] if isinstance(baseline_metrics, dict) else None
            result["comparison"] = {
                "baseline_experiment_id": baseline_experiment_id,
                "baseline_status": baseline["status"],
                "baseline_expected_value": baseline_score,
                "expected_value_delta": (
                    score - baseline_score
                    if type(score) in (float, int) and type(baseline_score) in (float, int) else None
                ),
            }
        return result

    def _get(self, experiment_id: str) -> tuple[dict[str, JSONValue], str]:
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
        return {
            "experiment_id": experiment_id,
            "strategy_id": details["strategy_id"],
            "config_id": details["config_id"],
            "version": details["version"],
            "seed": int(details["seed"]),
            "timeout_seconds": int(details["timeout_seconds"]),
            "status": summary.get("status", job.status),
            "elapsed_seconds": job.elapsed_seconds,
            "solver_elapsed_seconds": summary.get("solver_elapsed_seconds"),
            "valid": summary.get("valid"),
            "metrics": summary.get("metrics"),
            "diagnostic": summary.get("diagnostic", diagnostic),
            "result_ref": summary.get("result_ref"),
        }, details["scene_digest"]


def _validate_profile(strategy: Strategy, profile: StrategyProfile) -> None:
    if type(profile.seed) is not int or profile.seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if type(profile.timeout_seconds) is not int or not 1 <= profile.timeout_seconds <= MAX_COMMAND_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer in [1, {MAX_COMMAND_TIMEOUT_SECONDS}]")
    try:
        Draft202012Validator(strategy.parameter_schema).validate(profile.parameters)
        json.dumps(profile.parameters, allow_nan=False)
    except (ValidationError, TypeError) as error:
        raise ValueError(str(error)) from error


def _result(status: str, elapsed: float, **summary: JSONValue) -> JobResult:
    return JobResult(
        output={"status": status, "next": "Use experiment_get with the job ID to inspect the evaluation."},
        result_summary={"status": status, "solver_elapsed_seconds": elapsed, **summary},
    )
