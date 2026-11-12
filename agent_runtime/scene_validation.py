"""Validate contest scenes and prepare their features for model input."""

import argparse
import json
import math
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from statistics import fmean, median

from agent_core import Workspace

from .attachments import store_attachment


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"{constant} is not a valid JSON value")


def _distribution(ordered: Sequence[int | float]) -> dict[str, int | float]:
    """Summarize sorted observations using nearest-rank tail percentiles."""
    count = len(ordered)
    return {
        "p10": round(ordered[(count - 1) // 10], 6),
        "median": round(median(ordered), 6),
        "p90": round(ordered[(9 * count - 1) // 10], 6),
    }


def extract_scene_features(text: str) -> dict[str, object]:
    """Use the rows of P as W and its columns as T, aligned with v."""
    scene = json.loads(text.removeprefix("\ufeff"), parse_constant=_reject_json_constant)
    if not isinstance(scene, dict):
        raise ValueError("The scene must be a JSON object")

    values = scene.get("v")
    if not isinstance(values, list) or not values:
        raise ValueError("v must be a non-empty array")
    for index, value in enumerate(values):
        if type(value) is int or (type(value) is float and math.isfinite(value)):
            continue
        raise ValueError(f"v[{index}] must be a finite number")

    matrix = scene.get("P")
    if not isinstance(matrix, list) or not matrix:
        raise ValueError("P must be a non-empty matrix")
    columns = len(values)
    workers = len(matrix)
    workers_per_task = [0] * columns
    best_probabilities = [0] * columns
    tasks_per_worker = []
    positive_probabilities = []
    for row_index, row in enumerate(matrix):
        if not isinstance(row, list) or len(row) != columns:
            raise ValueError(f"P[{row_index}] must be an array of length {columns}")
        candidate_tasks = 0
        for column_index, value in enumerate(row):
            if type(value) not in (int, float) or not 0 <= value <= 1:
                raise ValueError(f"P[{row_index}][{column_index}] must be a number in [0, 1]")
            if value > 0:
                positive_probabilities.append(value)
                candidate_tasks += 1
                workers_per_task[column_index] += 1
                best_probabilities[column_index] = max(best_probabilities[column_index], value)
        tasks_per_worker.append(candidate_tasks)

    positive_pairs = len(positive_probabilities)
    ordered_best_probabilities = sorted(best_probabilities)
    ordered_values = sorted(values)
    value_distribution = _distribution(ordered_values)
    total_value = sum(values)
    unreachable_value = sum(
        value for value, best in zip(values, best_probabilities) if best == 0
    )
    if any(
        type(value) is float and not math.isfinite(value)
        for value in (total_value, unreachable_value, value_distribution["median"])
    ):
        raise ValueError("Scene value statistics must be finite")

    top_count = (columns + 9) // 10
    top_value_share = None
    if ordered_values[0] >= 0 and total_value > 0:
        top_value_share = round(sum(ordered_values[-top_count:]) / total_value, 6)
    return {
        "T": {
            "count": columns,
            "tasks_without_candidates": workers_per_task.count(0),
            "tasks_with_one_candidate": workers_per_task.count(1),
            "candidate_workers_per_task": _distribution(sorted(workers_per_task)),
        },
        "W": {
            "count": workers,
            "workers_without_candidates": tasks_per_worker.count(0),
            "candidate_tasks_per_worker": _distribution(sorted(tasks_per_worker)),
        },
        "P": {
            "positive_fraction": round(positive_pairs / (workers * columns), 6),
            "positive_probability_mean": (
                round(fmean(positive_probabilities), 6) if positive_pairs else None
            ),
            "positive_probability_median": (
                round(median(positive_probabilities), 6) if positive_pairs else None
            ),
            "best_probability_per_task": {
                "min": ordered_best_probabilities[0],
                **_distribution(ordered_best_probabilities),
            },
        },
        "V": {
            "total_value": total_value,
            "unreachable_value": unreachable_value,
            "value_distribution": value_distribution,
            "top_10_percent_value_share": top_value_share,
        },
    }


def prepare_scene_input(value: object, workspace: Workspace) -> str:
    """Build user message text and keep exact source data for workspace tools."""
    if not isinstance(value, dict):
        raise ValueError("user_turn.json_input must be an object")
    filename = value.get("filename")
    text = value.get("text")
    if not isinstance(filename, str) or not filename.lower().endswith(".json"):
        raise ValueError("json_input.filename must end in .json")
    if not isinstance(text, str):
        raise ValueError("json_input.text must be a string")

    features = extract_scene_features(text)
    source = store_attachment(
        BytesIO(text.encode("utf-8")),
        workspace,
        filename,
        "application/json",
    )
    return json.dumps(
        {"filename": source["filename"], "source_path": source["path"], **features},
        ensure_ascii=False,
        indent=2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Path to a scene JSON file")
    path = parser.parse_args().path
    try:
        features = extract_scene_features(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        parser.exit(1, f"{path}: {error}\n")
    print(json.dumps(features, indent=2))


if __name__ == "__main__":
    main()
