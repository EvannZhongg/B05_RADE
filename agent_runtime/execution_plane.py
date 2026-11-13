"""Workspace resources and their replaceable Session binding."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_core import (
    Agent,
    AgentConfig,
    ContextWindow,
    HostCommandExecutor,
    JobManager,
    MemoryManager,
    Workspace,
    WorkspaceInstructions,
    SkillRegistry,
    ToolCatalog,
)
from agent_core.llm import LLMProvider
from agent_core.hooks import HookRunner
from agent_core.tools import ToolSet

from .scene_tool import QuerySceneTool, SceneToolContext

if TYPE_CHECKING:
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
    catalog: ToolCatalog
    skills: SkillRegistry
    prompts: "PromptTemplates"
    plugin_agents: tuple["PluginAgent", ...]
    command_executor: HostCommandExecutor
    mcp: "McpClientManager"
    hooks: HookRunner = field(default_factory=HookRunner)
    role_tools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    memory: MemoryManager | None = None
    agent: Agent | None = None
    tool_context: SceneToolContext | None = None
    jobs: JobManager | None = None
    context_window: ContextWindow | None = None
    runtime_warnings: tuple[str, ...] = ()

    def select_main_tools(self, context: SceneToolContext) -> ToolSet:
        return self.catalog.select(
            (
                *self.agent_config.tools.enabled,
                "read_image",
                "read_skill",
                "update_plan",
                *self.mcp.tool_names,
                "remember",
                QuerySceneTool.name,
            ),
            context,
        )

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
        jobs = self.jobs
        self.jobs = None
        self.agent = None
        self.tool_context = None
        self.memory = None
        self.context_window = None
        if jobs is not None:
            jobs.close()

    def close(self) -> None:
        try:
            self.unbind_session()
        finally:
            self.mcp.close()


__all__ = ["ExecutionPlane"]
