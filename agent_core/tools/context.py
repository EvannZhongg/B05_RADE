from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..session_paths import default_sessions_directory
from ..workspace import Workspace
from .base import JSONValue

if TYPE_CHECKING:
    from ..hooks import HookRunner, HookInvocation, HookCompletedEvent
    from ..execution.executor import CommandExecutor
    from ..execution.process import CancellationSignal
    from ..mcp.manager import McpClientManager
    from ..plan import PlanManager
    from ..session import Session
    from ..skills import SkillRegistry
    from ..subagent import SubagentRuntime
    from ..jobs import JobManager
    from ..memory import MemoryManager


@dataclass(frozen=True)
class ToolExecutionContext:
    """The Runtime dependencies handed to every Tool call.

    The Runtime builds one context per Agent: the heavy dependencies are
    the Runtime's own singletons, while ``session`` names the Agent the
    call belongs to. Everything here is read-only for the duration of a
    call, so parallel invocations of one shared Tool stay isolated.

    A dependency left as ``None`` makes the tools that need it unavailable
    rather than failing at call time; see :meth:`Tool.available`.
    """

    workspace: Workspace
    session: "Session"
    sessions_directory: Path = field(
        default_factory=default_sessions_directory
    )
    command_executor: "CommandExecutor | None" = None
    vision_input: bool = False
    mcp: "McpClientManager | None" = None
    subagents: "SubagentRuntime | None" = None
    jobs: "JobManager | None" = None
    cancellation: "CancellationSignal | None" = None
    skills: "SkillRegistry | None" = None
    plan: "PlanManager | None" = None
    memory: "MemoryManager | None" = None
    hooks: "HookRunner | None" = None
    hook_invocation: "HookInvocation | None" = None
    on_hook_event: "Callable[[HookCompletedEvent], None] | None" = None
    agent_role: str = "main"
    parent_session_id: str | None = None
    artifact_directory: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sessions_directory",
            self.sessions_directory.expanduser().resolve(),
        )
