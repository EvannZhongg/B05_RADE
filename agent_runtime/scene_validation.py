"""Validate contest scenes and prepare their features for model input."""

import argparse
import json
import math
from io import BytesIO
from pathlib import Path

from agent_core import Workspace

from .attachments import store_attachment


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"{constant} is not a valid JSON value")


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
    for row_index, row in enumerate(matrix):
        if not isinstance(row, list) or len(row) != columns:
            raise ValueError(f"P[{row_index}] must be an array of length {columns}")
        for column_index, value in enumerate(row):
            if type(value) not in (int, float) or not 0 <= value <= 1:
                raise ValueError(f"P[{row_index}][{column_index}] must be a number in [0, 1]")

    workers = len(matrix)
    tasks_per_worker = [sum(map(bool, row)) for row in matrix]
    workers_per_task = []
    best_probabilities = []
    for probabilities in zip(*matrix):
        workers_per_task.append(sum(map(bool, probabilities)))
        best_probabilities.append(max(probabilities))

    positive_pairs = sum(tasks_per_worker)
    pair_count = workers * columns
    top_tasks = sorted(range(columns), key=values.__getitem__, reverse=True)[:5]
    return {
        "T": {
            "count": columns,
            "without_positive_probability": workers_per_task.count(0),
            "min_candidate_workers": min(workers_per_task),
            "max_candidate_workers": max(workers_per_task),
            "mean_candidate_workers": round(positive_pairs / columns, 6),
        },
        "W": {
            "count": workers,
            "without_positive_probability": tasks_per_worker.count(0),
            "min_candidate_tasks": min(tasks_per_worker),
            "max_candidate_tasks": max(tasks_per_worker),
            "mean_candidate_tasks": round(positive_pairs / workers, 6),
        },
        "P": {
            "positive_fraction": round(positive_pairs / pair_count, 6),
            "mean_probability": round(sum(map(sum, matrix)) / pair_count, 6),
            "max_probability": max(best_probabilities),
        },
        "V": {
            "min_value": min(values),
            "max_value": max(values),
            "top_tasks_by_value": [
                {
                    "task_id": index + 1,
                    "value": values[index],
                    "candidate_workers": workers_per_task[index],
                    "best_probability": best_probabilities[index],
                }
                for index in top_tasks
            ],
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
