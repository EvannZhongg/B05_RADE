"""Load contest scenes and prepare them for a runtime turn."""

import json
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from agent_core import Workspace
from rade_tasks.scene import SceneState

from .attachments import store_attachment


SCENE_FILENAME = re.compile(r"scene[12]_[1-5]\.json")


@dataclass(frozen=True)
class SceneInput:
    text: str
    state: SceneState


def list_scene_inputs(input_directory: Path) -> list[str]:
    if input_directory.resolve() != input_directory:
        raise ValueError("The input directory must be under the application root")
    try:
        return sorted(
            path.name for path in input_directory.iterdir()
            if SCENE_FILENAME.fullmatch(path.name) and path.resolve() == path and path.is_file()
        )
    except FileNotFoundError:
        return []


def read_scene_input(input_directory: Path, filename: str) -> bytes:
    if not SCENE_FILENAME.fullmatch(filename):
        raise ValueError("Expected a scene filename such as scene1_1.json")
    path = input_directory / filename
    if path.resolve() != path or not path.is_file():
        raise FileNotFoundError(f"Scene is unavailable in input/: {filename}")
    return path.read_bytes()


def prepare_scene_input(input_directory: Path, filename: object, workspace: Workspace) -> SceneInput:
    """Build user message text and keep exact source data for workspace tools."""
    if not isinstance(filename, str):
        raise ValueError("user_turn.scene_input must be a scene filename")
    data = read_scene_input(input_directory, filename)
    state = SceneState(data.decode("utf-8"))
    source = store_attachment(
        BytesIO(data),
        workspace,
        filename,
        "application/json",
    )
    text = json.dumps(
        {"filename": source["filename"], "source_path": source["path"], **state.summary()},
        ensure_ascii=False,
        indent=2,
    )
    return SceneInput(text, state)
