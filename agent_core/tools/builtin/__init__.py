from .apply_patch import ApplyPatchTool
from .edit_file import EditFileTool
from .list_directory import ListDirectoryTool
from .read_file import ReadFileTool
from .read_image import ReadImageTool
from .read_skill import ReadSkillTool
from .search_files import SearchFilesTool
from .shell import ShellTool
from .subagent import SubagentTool
from .job_control import JobControlTool
from .write_file import WriteFileTool
from .update_plan import UpdatePlanTool
from .remember import RememberTool


def builtin_catalog():
    """Return the shared catalog of every builtin Tool.

    Instances are stateless, so one catalog serves every Agent of a
    Runtime; which of them a role may call is decided by selection, not
    by building a different catalog.
    """
    from ..catalog import ToolCatalog

    return ToolCatalog(
        (
            ReadFileTool(),
            ApplyPatchTool(),
            EditFileTool(),
            WriteFileTool(),
            SearchFilesTool(),
            ListDirectoryTool(),
            ShellTool(),
            ReadImageTool(),
            ReadSkillTool(),
            SubagentTool(),
            JobControlTool(),
            UpdatePlanTool(),
            RememberTool(),
        )
    )


__all__ = [
    "ApplyPatchTool",
    "EditFileTool",
    "ListDirectoryTool",
    "ReadFileTool",
    "ReadImageTool",
    "ReadSkillTool",
    "SearchFilesTool",
    "ShellTool",
    "SubagentTool",
    "JobControlTool",
    "UpdatePlanTool",
    "RememberTool",
    "WriteFileTool",
    "builtin_catalog",
]
