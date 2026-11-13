"""Contracts and registration for task allocation strategies."""

from dataclasses import dataclass
from typing import Protocol

from .scene import SceneSnapshot


class Solver(Protocol):
    """Solve using Python directly or by adapting a separate executable."""

    def __call__(
        self, scene: SceneSnapshot, parameters: dict[str, object], *, seed: int,
    ) -> list[int | None]:
        """Return one zero-based task index or None for each original row of P."""
        ...


@dataclass(frozen=True)
class StrategyProfile:
    parameters: dict[str, object]
    seed: int
    timeout_seconds: int


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    version: str
    description: str
    entrypoint: str  # An installed module and Solver function, as module:function.
    parameter_schema: dict[str, object]
    profiles: dict[str, StrategyProfile]


BUILTIN_STRATEGIES: tuple[Strategy, ...] = ()
