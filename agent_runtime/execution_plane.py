"""Workspace resources and their replaceable Session binding."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_core import (
    Agent,
    AgentConfig,
    ContextWindow,
    ExecutionRouter,
    JobManager,
    MemoryManager,
    Workspace,
    WorkspaceInstructions,
    SkillRegistry,
    ToolCatalog,
)
from agent_core.llm import LLMProvider
from agent_core.hooks import HookRunner

if TYPE_CHECKING:
    from .session_title import SessionTitleTask
    from agent_core.mcp.manager import McpClientManager
    from .config import PromptTemplates
    from .plugins import PluginAgent


@dataclass(eq=False)
class ExecutionPlane:
    """One resource assembly, with fresh Agent state for each Session binding."""

    workspace: Workspace
    provider_name: str
    agent_config: AgentConfig
    configuration_fingerprint: str
    instructions: WorkspaceInstructions
    provider: LLMProvider
    fast_provider: LLMProvider
    catalog: ToolCatalog
    skills: SkillRegistry
    prompts: "PromptTemplates"
    plugin_agents: tuple["PluginAgent", ...]
    execution_router: ExecutionRouter
    mcp: "McpClientManager"
    hooks: HookRunner = field(default_factory=HookRunner)
    role_tools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    memory: MemoryManager | None = None
    title_task: "SessionTitleTask | None" = None
    agent: Agent | None = None
    jobs: JobManager | None = None
    context_window: ContextWindow | None = None
    runtime_warnings: tuple[str, ...] = ()

    def matches(
        self,
        *,
        workspace: Workspace,
        provider_name: str,
        agent_config: AgentConfig,
        configuration_fingerprint: str,
        instructions: WorkspaceInstructions,
    ) -> bool:
        return (
            self.workspace == workspace
            and self.provider_name == provider_name
            and self.agent_config == agent_config
            and self.configuration_fingerprint
            == configuration_fingerprint
            and self.instructions.fingerprint == instructions.fingerprint
        )

    def unbind_session(self) -> None:
        title_task, jobs = self.title_task, self.jobs
        self.title_task = None
        self.jobs = None
        self.agent = None
        self.memory = None
        self.context_window = None
        try:
            if title_task is not None:
                title_task.close()
        finally:
            if jobs is not None:
                jobs.close()

    def close(self) -> None:
        try:
            self.unbind_session()
        finally:
            try:
                self.mcp.close()
            finally:
                self.execution_router.close()


__all__ = ["ExecutionPlane"]
