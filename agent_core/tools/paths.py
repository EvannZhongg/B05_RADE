"""Path resolution shared by the tools that read run files."""

from pathlib import Path

from ..path_utils import relative_to_root
from .context import ToolExecutionContext


def resolve_readable_path(
    path: str,
    context: ToolExecutionContext,
) -> tuple[Path, str]:
    """Resolve a path relative to the current run's files."""
    workspace = context.workspace
    resolved = workspace.resolve_path(path)
    return resolved, relative_to_root(resolved, workspace.path).as_posix()
