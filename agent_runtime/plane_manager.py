"""Assembly, invalidation and resource ownership for one execution plane."""

from contextlib import ExitStack
from functools import partial
from pathlib import Path
from typing import Callable
from agent_core import (
    Agent,
    JobManager,
    JobStatusEvent,
    MemoryManager,
    MemoryReconciler,
    McpConfig,
    SubagentRole,
    SubagentRoleRegistry,
    SubagentRuntime,
    HostCommandExecutor,
    ToolExecutionContext,
    Workspace,
    WorkspaceInstructions,
    builtin_catalog,
    load_agent_config,
    merge_mcp_servers,
    Session,
)
from agent_core.prompting import render_system_prompt
from agent_core.llm import LLMProvider
from agent_core.tools import ROLE_TOOL_NAMES
from agent_core.hooks import HookRunner
from agent_core.execution.process import CancellationSignal

from .config import (
    ModelConfig,
    load_config,
    load_prompt_templates,
    load_runtime_skills,
    memory_store,
)
from .execution_plane import ExecutionPlane
from .instructions import load_workspace_instructions
from .plugins import PluginAgent, PluginManager
from .role_tools import resolve_role_tools
from .settings import EnvironmentReloader, configuration_fingerprint
from .callbacks import RuntimeCallbacks
from .session_controller import SessionRuntimeController


class ExecutionPlaneManager:
    def __init__(self, config_directory: Path, environment: EnvironmentReloader,
                 callbacks: RuntimeCallbacks) -> None:
        self._config_directory = config_directory
        self._config_path = config_directory / "provider_config.json"
        self._agent_config_path = config_directory / "agent_config.json"
        self._environment = environment
        self.callbacks = callbacks
        self.current: ExecutionPlane | None = None

    def ensure(
        self, session: SessionRuntimeController, *, force: bool = False,
        cancellation: CancellationSignal | None = None,
    ) -> ExecutionPlane:
        if (
            session.session is None
            or session.store is None
            or session.workspace is None
            or session.provider_name is None
        ):
            raise RuntimeError("received 'user_turn' before 'open_session'")

        self._environment.reload()
        agent_config = load_agent_config(self._agent_config_path)
        config_fingerprint = configuration_fingerprint(
            self._config_directory
        )
        instructions = load_workspace_instructions(
            self._config_directory,
            session.workspace,
            agent_config.workspace_instruction_files,
        )
        plugins = PluginManager.discover(self._config_directory / "plugins")
        plugin_mcp_servers, mcp_warnings = (
            plugins.load_mcp_servers() if agent_config.mcp.enabled else ((), ())
        )
        mcp_config = McpConfig(
            enabled=agent_config.mcp.enabled,
            servers=merge_mcp_servers((agent_config.mcp.servers, plugin_mcp_servers)),
        )
        plane = self.current
        if (
            not force and plane is not None and plane.mcp.config == mcp_config
            and plane.matches(
                workspace=session.workspace,
                provider_name=session.provider_name,
                agent_config=agent_config,
                configuration_fingerprint=config_fingerprint,
                instructions=instructions,
            )
        ):
            if plane.agent is None:
                self._bind_session(plane, session)
            return plane
        if plane is not None:
            self.close()

        self.callbacks.on_phase("starting", ())
        config_path = self._config_path
        workspace = session.workspace
        config = load_config(config_path, session.provider_name)
        plugin_agents, agent_warnings = plugins.load_agents()
        hook_definitions, hook_warnings = plugins.load_hooks()
        from agent_core.mcp.manager import McpClientManager

        mcp = McpClientManager(
            mcp_config, workspace.path,
            on_status=self.callbacks.on_mcp_status,
        )
        with ExitStack() as startup:
            startup.callback(mcp.close)
            mcp.start()
            skills = load_runtime_skills(self._config_directory, plugins)
            prompts = load_prompt_templates(self._config_directory / "prompts")

            main_provider = self._provider_for(config, workspace, agent_config)
            # One catalog of stateless Tool instances is shared by the main
            # Agent and by every sub-agent role.
            catalog = builtin_catalog()
            catalog = catalog.extend(mcp.wait_ready(cancellation))
            role_tools = {}
            if agent_config.tools.is_enabled("subagent"):
                for name, role in agent_config.subagent_roles.items():
                    if role.enabled:
                        try:
                            role_tools[name] = resolve_role_tools(
                                role.tools.enabled, role.mcp, catalog, mcp,
                            )
                        except ValueError as error:
                            raise ValueError(f"subagent role '{name}': {error}") from error
            resolved_agents = []
            tool_warnings = []
            for agent in plugin_agents:
                try:
                    tools = resolve_role_tools(
                        ROLE_TOOL_NAMES if agent.tools is None else agent.tools,
                        {}, catalog, mcp,
                    )
                except ValueError as error:
                    tool_warnings.append(
                        f"Skipping Agent '{agent.name}' at '{agent.source}': "
                        f"{error}. Check MCP enablement, "
                        "server status, tools.enabled and the qualified tool name."
                    )
                    continue
                role_tools[agent.name] = tools
                resolved_agents.append(agent)

            plane = ExecutionPlane(
                workspace=workspace,
                provider_name=session.provider_name,
                agent_config=agent_config,
                configuration_fingerprint=config_fingerprint,
                instructions=instructions,
                provider=main_provider,
                catalog=catalog,
                skills=skills,
                prompts=prompts,
                plugin_agents=tuple(resolved_agents),
                command_executor=HostCommandExecutor(workspace.path),
                mcp=mcp,
                hooks=HookRunner(hook_definitions),
                role_tools=role_tools,
                runtime_warnings=(
                    *plugins.warnings,
                    *mcp_warnings,
                    *agent_warnings,
                    *hook_warnings,
                    *tool_warnings,
                    *skills.warnings,
                ),
            )
            # The completed plane now owns the acquired resources.
            startup.pop_all()
        self._bind_session(plane, session)
        self.current = plane
        return plane

    def _bind_session(self, plane: ExecutionPlane, session: SessionRuntimeController) -> None:
        try:
            agent_config = plane.agent_config
            workspace = plane.workspace
            if session.session is None:
                raise RuntimeError("session is not initialized")
            main_provider = plane.provider
            catalog = plane.catalog
            skills = plane.skills
            prompts = plane.prompts
            instructions = plane.instructions
            plugin_agents = plane.plugin_agents
            mcp = plane.mcp
            store = None
            memory_context = None
            if agent_config.memory.enabled:
                store = memory_store(self._config_directory)
                store.initialize(workspace.path)
                memory_context = store.load(workspace.path)
            memory = None

            def create_memory(
                owner: Session,
            ) -> MemoryManager:
                assert store is not None
                return MemoryManager(
                    workspace.path,
                    store,
                    MemoryReconciler(
                        main_provider,
                        workspace_prompt=prompts.workspace_memory,
                        workspace_max_tokens=agent_config.memory.workspace_max_tokens,
                        on_auxiliary_call=partial(
                            owner.auxiliary_model_completed, model=main_provider.model_name,
                        ),
                    ),
                )

            if memory_context is not None:
                memory = create_memory(session.session)

            jobs = JobManager(session.session)
            plane.jobs = jobs
            jobs.set_update_callback(
                lambda update: self.callbacks.on_event(
                    JobStatusEvent(
                        job_id=update.job_id,
                        kind=update.kind,
                        status=update.status,
                        details=update.details,
                    ),
                    update.turn_id,
                )
            )
            subagents = self._subagent_runtime(
                agent_config,
                catalog,
                main_provider,
                prompts.subagent,
                prompts.consolidator,
                instructions,
                plugin_agents,
                plane.role_tools,
                create_memory if store is not None else None,
            )
            context = ToolExecutionContext(
                workspace=workspace,
                session=session.session,
                sessions_directory=session.sessions_directory,
                command_executor=plane.command_executor,
                vision_input=(
                    "image" in main_provider.capabilities.input_modalities
                ),
                mcp=mcp,
                subagents=subagents,
                jobs=jobs,
                skills=skills,
                plan=session.plan,
                memory=memory,
                hooks=plane.hooks,
                on_hook_event=lambda event: self.callbacks.on_event(event, session.session.current_turn_id),
            )
            tools = catalog.select(
                (
                    *agent_config.tools.enabled,
                    "read_image",
                    "read_skill",
                    "update_plan",
                    *mcp.tool_names,
                    "remember",
                ),
                context,
            )
            agent = Agent(
                provider=main_provider,
                session=session.session,
                system_prompt=render_system_prompt(
                    prompts.system,
                    workspace,
                    skills,
                    instructions=instructions,
                    mcp_instructions=mcp.instructions_for(
                        tool.name for tool in tools.definitions
                    ),
                ),
                consolidator_prompt=prompts.consolidator,
                memory_context=memory_context,
                config=agent_config,
                tools=tools,
                context=context,
            )
            plane.memory = memory
            plane.agent = agent
            plane.context_window = agent.context_window()
        except BaseException:
            if self.current is plane:
                self.current = None
            plane.close()
            raise

    def unbind_session(self) -> None:
        if self.current is not None:
            self.current.unbind_session()

    def _provider_for(
        self,
        config: ModelConfig,
        workspace: Workspace,
        agent_config,
    ) -> LLMProvider:
        from agent_core.providers import OpenAIProvider

        return OpenAIProvider(
            model=config.model,
            api=config.api,
            base_url=config.url,
            api_key=config.key,
            max_context_tokens=config.max_context_tokens,
            media_root=workspace.path,
            request_timeout_seconds=(
                agent_config.provider.request_timeout_seconds
            ),
            max_retries=agent_config.provider.max_retries,
            reasoning_effort=config.reasoning_effort,
            reasoning_parameters=config.reasoning_parameters,
        )

    def _subagent_runtime(
        self,
        agent_config,
        catalog,
        provider: LLMProvider,
        subagent_prompt_template: str,
        consolidator_prompt: str,
        workspace_instructions: WorkspaceInstructions,
        plugin_agents: tuple[PluginAgent, ...],
        role_tools: dict[str, tuple[str, ...]],
        memory_factory: Callable[[Session], MemoryManager] | None,
    ) -> SubagentRuntime | None:
        """Build the sub-agent runtime, or None when no role is configured."""
        if not agent_config.tools.is_enabled("subagent"):
            return None
        roles = [
            SubagentRole(
                name=name,
                description=role.description,
                tools=role_tools[name],
                provider=provider,
            )
            for name, role in agent_config.subagent_roles.items()
            if role.enabled
        ]
        for agent in plugin_agents:
            roles.append(
                SubagentRole(
                    name=agent.name,
                    description=agent.description,
                    instructions=agent.instructions,
                    tools=role_tools[agent.name],
                    provider=provider,
                )
            )
        registry = SubagentRoleRegistry(roles)
        if not registry:
            return None

        return SubagentRuntime(
            config=agent_config,
            catalog=catalog,
            roles=registry,
            subagent_prompt_template=subagent_prompt_template,
            consolidator_prompt=consolidator_prompt,
            workspace_instructions=workspace_instructions,
            memory_factory=memory_factory,
        )


    def close(self) -> None:
        plane = self.current
        self.current = None
        if plane is not None:
            plane.close()
