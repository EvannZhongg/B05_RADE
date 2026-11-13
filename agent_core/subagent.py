"""Sub-agent roles and the Runtime that runs them.

A role is a name, a description the parent model reads, private instructions
for the child, and the set of Tool names the role may use. Roles share the
Runtime's Tool Catalog, so adding one costs configuration rather than a new
Tool implementation.
"""
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable, Iterable

from .config import AgentConfig
from .llm import LLMProvider
from .memory import MemoryManager
from .prompting import render_subagent_prompt
from .session import Session
from .session_store import JsonlSessionStore
from .tools.catalog import ToolCatalog
from .tools.context import ToolExecutionContext
from .tools.base import JSONValue, ToolPolicy
from .execution.process import CancellationSignal
from .turn_control import TurnControl
from .session_paths import default_runs_directory, session_directory
from .runtime_instructions import RuntimeInstructions

if TYPE_CHECKING:
    from .agent import AgentEvent


@dataclass(frozen=True)
class SubagentRole:
    """A sub-agent role: its purpose, instructions, tools, and providers.

    Each role carries its own providers, so one runtime can host a cheap
    read-only role and an expensive editing role side by side.
    """

    name: str
    description: str
    tools: tuple[str, ...]
    provider: LLMProvider
    instructions: str = ""


class SubagentRoleRegistry:
    """The Runtime's roles, keyed by name and ordered for the schema."""

    def __init__(self, roles: Iterable[SubagentRole] = ()) -> None:
        self._roles: dict[str, SubagentRole] = {}
        for role in roles:
            if role.name in self._roles:
                raise ValueError(
                    f"subagent role '{role.name}' is already registered"
                )
            self._roles[role.name] = role

    def __bool__(self) -> bool:
        return bool(self._roles)

    def __iter__(self):
        return iter(self._roles.values())

    def get(self, name: str) -> SubagentRole:
        role = self._roles.get(name)
        if role is None:
            known = ", ".join(self._roles) or "none"
            raise ValueError(
                f"unknown subagent role '{name}'; available roles: {known}"
            )
        return role


class SubagentRuntime:
    """Runs a child Agent for a role, isolated from the parent.

    The Runtime owns only Runtime-level dependencies; each role brings its
    own providers. Everything that belongs to one invocation — which
    Session is the parent, which workspace, where transcripts go — arrives
    with the parent's :class:`ToolExecutionContext`, so a single instance
    is shared by the main Agent and by parallel sub-agent calls.
    """

    def __init__(
        self,
        config: AgentConfig,
        catalog: ToolCatalog,
        roles: SubagentRoleRegistry,
        subagent_prompt_template: str,
        consolidator_prompt: str,
        runtime_instructions: RuntimeInstructions,
        *,
        tool_policy: ToolPolicy | None = None,
        memory_factory: Callable[[Session], MemoryManager] | None = None,
    ) -> None:
        self._config = config
        self._catalog = catalog
        self._roles = roles
        self._subagent_prompt_template = subagent_prompt_template
        self._consolidator_prompt = consolidator_prompt
        self._runtime_instructions = runtime_instructions
        self._tool_policy = tool_policy
        self._memory_factory = memory_factory

    @property
    def roles(self) -> SubagentRoleRegistry:
        return self._roles

    def run(
        self,
        role_name: str,
        task: str,
        parent: ToolExecutionContext,
        cancellation: CancellationSignal | None = None,
        *,
        on_progress: Callable[[dict[str, JSONValue]], None] | None = None,
    ) -> str:
        from .agent import Agent

        role = self._roles.get(role_name)
        session = Session()
        memory = (
            self._memory_factory(session)
            if self._memory_factory is not None else None
        )
        store = self._store(parent)
        artifact_directory = parent.workspace.path / "artifacts" / "subagents" / session.session_id
        # A child never reaches the sub-agent runtime, so it cannot spawn
        # a grandchild: recursion is bounded by the context, not by
        # rewriting the child's tool configuration.
        context = replace(
            parent,
            session=session,
            vision_input=(
                "image" in role.provider.capabilities.input_modalities
            ),
            subagents=None,
            jobs=None,
            plan=None,
            memory=memory,
            cancellation=cancellation,
            agent_role=role.name,
            parent_session_id=parent.session.session_id,
            artifact_directory=artifact_directory,
        )
        tools = self._catalog.select(
            (
                *role.tools,
                "read_image",
                "read_skill",
                "remember",
            ),
            context,
            policy=self._tool_policy,
        )
        child = Agent(
            provider=role.provider,
            session=session,
            system_prompt=render_subagent_prompt(
                self._subagent_prompt_template,
                role,
                parent.skills,
                instructions=self._runtime_instructions,
                mcp_instructions=(
                    context.mcp.instructions_for(tool.name for tool in tools.definitions)
                    if context.mcp is not None else ()
                ),
            ),
            consolidator_prompt=self._consolidator_prompt,
            memory_context=(
                memory.store.load(memory.run_id) if memory is not None else None
            ),
            config=self._config,
            tools=tools,
            context=context,
        )
        with store.open_journal(session.session_id) as sink:
            session.attach_journal_sink(sink)
            try:
                result = child.run(
                    task,
                    on_event=(
                        _SubagentProgress(on_progress) if on_progress is not None else None
                    ),
                    turn_control=(
                        TurnControl(external_cancelled=lambda: cancellation.cancelled)
                        if cancellation is not None
                        else None
                    ),
                    input_source="delegation",
                )
                if memory is not None:
                    memory.reconcile_pending()
            finally:
                session.attach_journal_sink(None)
        # Full report delivery is separate from the bounded progress snapshots.
        return result.content

    def _store(self, parent: ToolExecutionContext) -> JsonlSessionStore:
        run = parent.run_directory or session_directory(default_runs_directory(), parent.session.session_id)
        return JsonlSessionStore(run / "subagents")


class _SubagentProgress:
    """Project execution events without exposing results or final response text."""

    def __init__(self, publish: Callable[[dict[str, JSONValue]], None]) -> None:
        self._publish = publish
        self._recent_tools: list[dict[str, JSONValue]] = []
        self._pending_text: tuple[int, str] | None = None
        self._text: str | None = None

    def __call__(self, event: "AgentEvent") -> None:
        from .agent import (
            AssistantMessageEvent, ToolBatchStartedEvent, ToolCallEvent, ToolResultEvent,
        )

        if isinstance(event, AssistantMessageEvent):
            # Only a following tool batch proves this is intermediate text.
            # Streaming text and final response text are never published.
            self._pending_text = (event.model_call_index, event.content[:1000])
            return
        if isinstance(event, ToolBatchStartedEvent):
            if (
                self._pending_text is not None
                and self._pending_text[0] == event.model_call_index
            ):
                self._text = self._pending_text[1]
            self._pending_text = None
            return
        elif isinstance(event, ToolCallEvent):
            self._recent_tools.append({
                "tool_call_id": event.tool_call.id,
                "name": event.tool_call.name,
                "status": "running",
            })
            self._recent_tools = self._recent_tools[-5:]
        elif isinstance(event, ToolResultEvent):
            for call in self._recent_tools:
                if call["tool_call_id"] == event.tool_result.tool_call_id:
                    call["status"] = (
                        "failed" if event.tool_result.error is not None else "completed"
                    )
                    break
            else:
                return
        else:
            return
        self._publish({
            "recent_tools": [dict(call) for call in self._recent_tools],
            "text": self._text,
        })
