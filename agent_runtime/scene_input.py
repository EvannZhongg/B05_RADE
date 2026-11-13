"""Load contest scenes and prepare them for a runtime turn."""

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from agent_core import Workspace
from agent_core.atomic import atomic_write_bytes
from rade_tasks.scene import SceneState


SCENE_FILENAME = re.compile(r"scene[12]_[1-5]\.json")
SceneMode = Literal["norethink", "rethink"]
DEFAULT_SCENE_MODE: SceneMode = "rethink"


@dataclass(frozen=True)
class SceneInput:
    text: str
    state: SceneState
    digest: str
    filename: str
    source_path: str


def validate_scene_mode(mode: object) -> SceneMode:
    if mode not in ("norethink", "rethink"):
        raise ValueError("mode must be norethink or rethink")
    return cast(SceneMode, mode)


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


def prepare_scene_input(input_directory: Path, filename: str, files: Workspace) -> SceneInput:
    """Build user message text and retain the exact scene input for file tools."""
    data = read_scene_input(input_directory, filename)
    state = SceneState(data.decode("utf-8"))
    source_path = f"scenes/{uuid4().hex}/{filename}"
    atomic_write_bytes(files.resolve_path(source_path), data)
    text = json.dumps(
        {"filename": filename, "source_path": source_path, **state.summary()},
        ensure_ascii=False,
        indent=2,
    )
    return SceneInput(text, state, sha256(data).hexdigest(), filename, source_path)
