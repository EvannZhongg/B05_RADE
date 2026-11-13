"""Path resolution shared by the tools that read run files."""

from pathlib import Path

from ..content import ImagePart
from ..media import ImageInfo, probe_image
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


def resolve_image(
    path: str,
    context: ToolExecutionContext,
) -> tuple[ImagePart, ImageInfo]:
    """Resolve *path* to an :class:`ImagePart` ready to send inline.

    The media type comes from the file's own bytes rather than its
    extension, because a provider that is handed ``image/png`` wrapping
    JPEG data rejects the whole request.

    """
    resolved, stored = resolve_readable_path(path, context)
    info = probe_image(resolved)
    return ImagePart(
        path=stored,
        mime_type=info.mime_type,
        filename=resolved.name,
        size_bytes=info.size_bytes,
    ), info
