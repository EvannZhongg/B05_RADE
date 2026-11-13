"""Application runtime used by the Bridge."""

from dataclasses import replace
from pathlib import Path

from agent_core import (
    AgentEvent, ContextWindowEvent, UnitCompletedEvent,
)

from .callbacks import RuntimeCallbacks
from .config import load_model_options
from .plane_manager import ExecutionPlaneManager
from .scene_task_runner import SceneTaskOutcome, SceneTaskRunner, latest_scene_task
from .session_controller import SessionRuntimeController
from .services import RuntimeServices


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
        self.scene_tasks = SceneTaskRunner(runtime_callbacks)

    def _require_idle(self) -> None:
        if self.scene_tasks.turn_id is not None:
            raise RuntimeError("cannot change runtime during a scene task")

    def open_session(self, session_id: str | None = None,
                     provider: str | None = None) -> bool:
        self._require_idle()
        with self.sessions.open_session(session_id, provider) as resumed:
            self.planes.close()
        session = self.sessions.session
        if resumed and session is not None:
            task = latest_scene_task(session)
            if task is not None and task["status"] == "running":
                recovered = latest_scene_task(session, recover=True)
                assert recovered is not None
                session.record_application_event("scene_task", recovered)
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
        client_turn_id = self.scene_tasks.turn_id if turn_id == self.scene_tasks.task_id else None
        self.callbacks.on_event(event, client_turn_id or turn_id)

    def run_scene_task(
        self, turn_id: str, filename: object, input_directory: Path, output_directory: Path,
    ) -> SceneTaskOutcome:
        session, files = self.sessions.session, self.sessions.files
        assert session is not None and files is not None
        return self.scene_tasks.run(
            turn_id, filename, input_directory, output_directory, session, files,
            lambda control: self.planes.ensure(self.sessions, cancellation=control),
        )

    def close(self) -> None:
        self.scene_tasks.shutdown()
        try:
            self.planes.close()
        finally:
            self.sessions.close()
