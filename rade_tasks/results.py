"""Select an evaluated allocation and serialize the scene result."""

import json
from collections.abc import Iterable
from dataclasses import dataclass

from .evaluation import evaluate
from .scene import SceneSnapshot


@dataclass(frozen=True)
class SceneResult:
    experiment_id: str
    allocation: list[int | None]
    expected_value: float

    def to_json(self) -> bytes:
        return (json.dumps({"allocation": self.allocation}, allow_nan=False) + "\n").encode("utf-8")


def select_result(scene: SceneSnapshot, candidates: Iterable[tuple[str, object]]) -> SceneResult:
    best = None
    for experiment_id, allocation in candidates:
        try:
            expected_value = float(evaluate(scene, allocation)["expected_value"])
        except (ValueError, OverflowError):
            continue
        if best is None or expected_value > best.expected_value:
            assert isinstance(allocation, list)
            best = SceneResult(experiment_id, allocation, expected_value)
    if best is None:
        raise ValueError("This scene task produced no valid allocation")
    return best
