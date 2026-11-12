"""Application runtime used by the Bridge."""

from dataclasses import replace
from pathlib import Path
from typing import Callable

from agent_core import (
    AgentEvent, AttachmentPart, ContextWindowEvent, UnitCompletedEvent, Workspace,
)

from .callbacks import RuntimeCallbacks
from .config import load_model_options
from .plane_manager import ExecutionPlaneManager
from .session_controller import SessionRuntimeController
from .services import RuntimeServices
from .turn_runner import TurnResult, TurnRunner


class RuntimeHost:
    def __init__(
        self,
        config_directory: Path,
        *,
        callbacks: RuntimeCallbacks | None = None,
        services: RuntimeServices | None = None,
    ) -> None:
        self.config_directory = config_directory.resolve()
        self.callbacks = callbacks or RuntimeCallbacks()
        runtime_callbacks = replace(self.callbacks, on_event=self.publish_event)
        services = services or RuntimeServices(self.config_directory)
        self.settings = services.settings
        self.planes = ExecutionPlaneManager(
            self.config_directory, services.environment,
            runtime_callbacks,
        )
        self.sessions = SessionRuntimeController(
            self.config_directory, services.environment, runtime_callbacks,
        )
        self.turns = TurnRunner(runtime_callbacks)

    def _require_idle(self) -> None:
        if self.turns.turn_id is not None:
            raise RuntimeError("cannot change runtime during a turn")

    def open_session(self, workspace: Path, session_id: str | None = None,
                     provider: str | None = None) -> bool:
        self._require_idle()
        with self.sessions.open_session(workspace, session_id, provider) as resumed:
            self.planes.unbind_session()
        return resumed

    def change_provider(self, provider: str) -> tuple[bool, str]:
        self._require_idle()
        if self.sessions.session is None:
            raise RuntimeError("session is not initialized")
        _, models = load_model_options(self.config_directory / "provider_config.json")
        if provider not in models:
            raise ValueError(f"provider '{provider}' is not configured")
        changed = provider != self.sessions.provider_name
        if changed:
            self.planes.close()
            self.sessions.change_provider(provider)
        return changed, models[provider]

    def change_workspace(self, path: Path) -> bool:
        self._require_idle()
        if self.sessions.session is None:
            raise RuntimeError("session is not initialized")
        workspace = Workspace(path)
        changed = workspace != self.sessions.workspace
        if changed:
            self.planes.close()
            self.sessions.change_workspace(workspace)
        return changed

    def refresh_runtime(self) -> tuple[str, str]:
        self._require_idle()
        try:
            plane = self.planes.ensure(self.sessions, force=True)
        except Exception:
            self.callbacks.on_phase("failed", ())
            raise
        self.callbacks.on_phase("idle", plane.runtime_warnings)
        plane.runtime_warnings = ()
        return plane.provider_name, plane.provider.model_name

    def publish_event(self, event: AgentEvent, turn_id: str) -> None:
        plane = self.planes.current
        if isinstance(event, UnitCompletedEvent):
            return
        if isinstance(event, ContextWindowEvent) and plane is not None:
            plane.context_window = event.window
        self.callbacks.on_event(event, turn_id)

    def run_turn(
        self, turn_id: str, text: str | Callable[[], str], *,
        attachments: Callable[[], tuple[AttachmentPart, ...]] = lambda: (),
    ) -> TurnResult:
        return self.turns.run_turn(
            turn_id, text,
            lambda: self.planes.ensure(self.sessions, cancellation=self.turns.control),
            attachments,
        )

    def close(self) -> None:
        self.turns.shutdown()
        try:
            self.planes.close()
        finally:
            self.sessions.close()
