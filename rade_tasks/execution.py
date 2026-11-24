"""Execute a registered strategy under its profile budget and evaluate its output."""

import json
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from importlib import import_module
from time import monotonic
from typing import Literal

from .algorithms.base import Solver, Strategy, StrategyProfile
from .evaluation import AllocationValidationError, evaluate
from .scene import SceneSnapshot


@dataclass(frozen=True)
class ExecutionResult:
    status: Literal["completed", "failed", "timed_out", "invalid"]
    elapsed_seconds: float
    allocation: list[int | None] | None = None
    metrics: dict[str, object] | None = None
    task_metrics: list[dict[str, object]] | None = None
    diagnostic: dict[str, object] | None = None


class StrategyTimeout(TimeoutError):
    def __init__(self, profile_timeout_seconds: int, budget_remaining_seconds: float) -> None:
        remaining = max(0.0, budget_remaining_seconds)
        message = (
            "Scene task budget expired before the solver started" if remaining == 0
            else f"Exceeded {min(profile_timeout_seconds, remaining):.3g} seconds"
        )
        self.diagnostic = {
            "code": "solver_timeout", "message": message,
            "profile_timeout_seconds": profile_timeout_seconds,
            "budget_remaining_seconds": remaining,
            "timed_out_by": "budget" if remaining <= profile_timeout_seconds else "profile",
        }
        super().__init__(message)


def execute_strategy(
    strategy: Strategy,
    profile: StrategyProfile,
    scene: SceneSnapshot,
    run_process: Callable[[tuple[str, ...], int, bytes], str],
) -> ExecutionResult:
    request = json.dumps({
        "P": scene.P,
        "v": scene.v,
        "parameters": profile.parameters,
        "seed": profile.seed,
    }, allow_nan=False).encode("utf-8")
    command = (sys.executable, "-P", "-m", "rade_tasks.execution", strategy.entrypoint)
    started = monotonic()
    try:
        output = run_process(command, profile.timeout_seconds, request)
    except StrategyTimeout as error:
        return ExecutionResult("timed_out", monotonic() - started, diagnostic=error.diagnostic)
    except RuntimeError as error:
        return ExecutionResult("failed", monotonic() - started,
                               diagnostic={"code": "solver_failed", "message": str(error)})
    elapsed = monotonic() - started

    try:
        allocation = json.loads(output)
        json.dumps(allocation, allow_nan=False)
        metrics = evaluate(scene, allocation, detailed=True)
    except AllocationValidationError as error:
        return ExecutionResult("invalid", elapsed, diagnostic={
            "code": "invalid_allocation", "message": str(error), "errors": error.errors,
        })
    except (ValueError, OverflowError) as error:
        return ExecutionResult("invalid", elapsed,
                               diagnostic={"code": "invalid_output", "message": str(error)})
    task_metrics = metrics.pop("task_metrics")
    return ExecutionResult("completed", elapsed, allocation=allocation, metrics=metrics,
                           task_metrics=task_metrics)


def main() -> None:
    module, function = sys.argv[1].split(":")
    request = json.load(sys.stdin.buffer)
    scene = SceneSnapshot(tuple(tuple(row) for row in request["P"]), tuple(request["v"]))
    try:
        with redirect_stdout(sys.stderr):
            solve: Solver = getattr(import_module(module), function)
            allocation = solve(scene, request["parameters"], seed=request["seed"])
        result = json.dumps(allocation, allow_nan=False)
    except Exception as error:
        sys.exit(f"{type(error).__name__}: {error}")
    print(result)


if __name__ == "__main__":
    main()
