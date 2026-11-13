"""Application services for interactive Agent execution."""

import os

# Startup must not depend on fetching a model catalog from GitHub. Provider
# metadata comes from the installed SDK; upgrading it updates the catalog.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from .host import RuntimeHost
from .plane_manager import ExecutionPlaneManager
from .session_controller import SessionRuntimeController
from .scene_task_runner import SceneTaskOutcome, SceneTaskRunner

__all__ = [
    "ExecutionPlaneManager",
    "RuntimeHost",
    "SessionRuntimeController",
    "SceneTaskOutcome",
    "SceneTaskRunner",
]
