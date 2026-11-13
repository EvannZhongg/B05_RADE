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
from .evaluation import evaluate
from .scene import SceneSnapshot


@dataclass(frozen=True)
class ExecutionResult:
    status: Literal["completed", "failed", "timed_out", "invalid"]
    elapsed_seconds: float
    allocation: list[int | None] | None = None
    metrics: dict[str, int | float] | None = None
    diagnostic: str | None = None


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
    except TimeoutError as error:
        return ExecutionResult("timed_out", monotonic() - started, diagnostic=str(error))
    except RuntimeError as error:
        return ExecutionResult("failed", monotonic() - started, diagnostic=str(error))
    elapsed = monotonic() - started

    try:
        allocation = json.loads(output)
        metrics = evaluate(scene, allocation)
    except (ValueError, OverflowError) as error:
        return ExecutionResult("invalid", elapsed, diagnostic=str(error))
    return ExecutionResult("completed", elapsed, allocation=allocation, metrics=metrics)


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
