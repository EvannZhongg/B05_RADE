"""One session channel's protocol adapter for the shared application runtime."""

from pathlib import Path
from queue import Queue
from threading import RLock
from typing import Callable, TYPE_CHECKING

from agent_core import AgentEvent, SessionInUseError, message_to_dict, runtime_error_info
from agent_core.usage import cache_usage_to_dict, usage_to_dict
if TYPE_CHECKING:
    from agent_core.mcp.manager import McpServerStatus
from agent_runtime.host import RuntimeHost
from agent_runtime.callbacks import RuntimeCallbacks
from agent_runtime.scene_input import list_scene_inputs
from agent_runtime.scene_task_runner import latest_scene_task
from agent_runtime.config import load_model_options
from agent_runtime.services import RuntimeServices
from .protocol import (
    event_to_message,
    context_window_to_dict,
    runtime_state_message,
    runtime_failure_to_dict,
    plan_updated_message,
    session_ready_message,
    session_open_failed_message,
    session_items_message,
    sessions_listed_message,
    settings_snapshot_message,
    settings_update_failed_message,
    scene_task_received_message,
)
from .event_stream import EventStream


class Bridge:
    def __init__(self, *, services: RuntimeServices,
                 input_directory: Path,
                 send: Callable[[dict[str, object]], None]) -> None:
        self._send = send
        self._input_directory = input_directory
        self._output_directory = input_directory.parent / "output"
        self._event_lock = RLock()
        self._messages: Queue[dict[str, object] | BaseException | None] = Queue()
        self._stream = EventStream()
        self._phase = "inactive"
        self._runtime_warnings: tuple[str, ...] = ()
        self._jobs: dict[str, dict[str, object]] = {}
        self._checkpoint_turn: str | None = None
        self.host = RuntimeHost(
            services.config_directory,
            services=services,
            callbacks=RuntimeCallbacks(
                on_event=self._emit_agent_event,
                on_phase=lambda phase, warnings: self._emit_runtime_state(phase, runtime_warnings=warnings),
                on_plan=lambda snapshot: self.emit(**plan_updated_message(snapshot)),
                on_mcp_status=self._emit_mcp_status,
            ),
        )

    def emit(self, type: str, **fields: object) -> None:
        with self._event_lock:
            message = {"type": type, **fields}
            if type == "job_status":
                job_id = str(fields["job_id"])
                if fields["status"] in {"submitted", "running"}:
                    self._jobs[job_id] = {
                        key: fields[key] for key in ("job_id", "kind", "status", "details")
                    }
                else:
                    self._jobs.pop(job_id, None)
            if type == "fatal":
                self._phase = "failed"
                self._jobs.clear()
            state = None
            if type == "runtime_state":
                state = message
            elif type == "session_open_failed" and self.host.sessions.session is not None:
                state = self._runtime_snapshot()
            elif type in {
                "provider_changed",
                "plan_updated", "context_window", "model_completed", "compression_completed", "job_status", "fatal",
                "scene_task_completed", "scene_task_cancelled", "scene_task_failed", "scene_task_timed_out",
                "runtime_refreshed", "runtime_refresh_failed",
            }:
                state = self._runtime_snapshot()
            items = None
            if type == "session_ready" or type in {
                "scene_task_completed", "scene_task_cancelled", "scene_task_failed", "scene_task_timed_out",
            } or (type == "context_window" and self._checkpoint_turn == fields.get("turn_id")):
                session = self.host.sessions.session
                if session is not None:
                    items = [message_to_dict(item, include_provider_data=False) for item in session.items]
                self._checkpoint_turn = None
            published = self._stream.publish(
                message, state=state, items=items,
            )
            self._send(published)


    def read_message(self) -> dict[str, object] | None:
        """Return the next command routed by the process transport."""
        message = self._messages.get()
        if isinstance(message, BaseException):
            raise message
        return message


    def submit(self, message: dict[str, object]) -> None:
        """Route one decoded command without blocking the process transport."""
        message_type = message["type"]
        if message_type == "user_steer":
            self._route_steer(message)
            return
        if message_type == "cancel":
            self._route_cancel(message)
            return
        if message_type == "scene_task_start":
            turn_id = message.get("turn_id")
            if isinstance(turn_id, str):
                self.host.scene_tasks.enqueue(turn_id)
                self.emit(**scene_task_received_message(turn_id))
        self._messages.put(message)

    def stop(self) -> None:
        self._route_shutdown()


    def _route_steer(self, message: dict[str, object]) -> None:
        steer_id = message.get("steer_id")
        turn_id = message.get("turn_id")
        text = message.get("text")
        if not isinstance(steer_id, str) or not steer_id:
            return
        accepted = (
            isinstance(turn_id, str) and isinstance(text, str) and bool(text.strip())
            and self.host.scene_tasks.steer(turn_id, steer_id, text.strip())
        )
        self.emit(
            "user_steer_received" if accepted else "user_steer_rejected",
            turn_id=turn_id,
            steer_id=steer_id,
            text=text if isinstance(text, str) else "",
        )


    def _route_cancel(self, message: dict[str, object]) -> None:
        turn_id = message.get("turn_id")
        if isinstance(turn_id, str):
            self.host.scene_tasks.cancel(turn_id)

    def _route_shutdown(self, *, notify_commands: bool = True) -> None:
        self.host.scene_tasks.shutdown()
        if notify_commands:
            self._messages.put(None)

    def _emit_runtime_state(
        self,
        phase: str,
        *,
        runtime_warnings: tuple[str, ...] = (),
    ) -> None:
        with self._event_lock:
            self._phase = phase
            self._runtime_warnings = runtime_warnings
            self.emit(**self._runtime_snapshot())

    def _runtime_snapshot(self) -> dict[str, object]:
        plane = self.host.planes.current
        session = self.host.sessions.session
        return runtime_state_message(
            phase=self._phase,
            scene_task=latest_scene_task(session) if session is not None else None,
            turn_id=self.host.scene_tasks.turn_id,
            provider=self.host.sessions.provider_name,
            cache_usage=cache_usage_to_dict(session.cache_usage) if session is not None else None,
            context_window=(
                context_window_to_dict(plane.context_window)
                if plane is not None and plane.context_window is not None
                else None
            ),
            jobs=list(self._jobs.values()),
            runtime_warnings=self._runtime_warnings,
            plan=self.host.sessions.plan.snapshot if self.host.sessions.plan is not None else None,
        )


    def _emit_agent_event(self, event: AgentEvent, turn_id: str) -> None:
        self.emit(**event_to_message(event, turn_id))


    def _emit_sessions(self) -> None:
        self.emit(**sessions_listed_message(self.host.sessions.store.list_sessions()))

    def _emit_session_items(self) -> None:
        """Answer ``load_session`` with the conversation to render."""
        if self.host.sessions.session is None:
            raise RuntimeError("received 'load_session' before 'open_session'")
        self.emit(
            **session_items_message(
                [message_to_dict(item, include_provider_data=False) for item in self.host.sessions.session.items]
            )
        )


    def _settings_request_id(
        self, message: dict[str, object]
    ) -> str | None:
        value = message.get("request_id")
        return value if isinstance(value, str) and value else None


    def _emit_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        try:
            self.emit(**settings_snapshot_message(
                self.host.settings.snapshot(), request_id
            ))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))


    def _save_provider_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        provider = message.get("provider")
        settings = message.get("settings")
        try:
            if not isinstance(provider, str) or not provider:
                raise ValueError("provider must be a non-empty string")
            revision = message.get("expected_revision")
            snapshot = self.host.settings.save_provider(
                provider,
                settings,
                revision if isinstance(revision, str) else None,
            )
            self.emit(**settings_snapshot_message(snapshot, request_id))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))


    def _save_agent_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        try:
            revision = message.get("expected_revision")
            snapshot = self.host.settings.save_agent(
                message.get("settings"),
                revision if isinstance(revision, str) else None,
            )
            self.emit(**settings_snapshot_message(snapshot, request_id))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))


    def _emit_mcp_status(self, status: "McpServerStatus") -> None:
        fields: dict[str, object] = {
            "server": status.server,
            "status": status.status,
        }
        if status.tool_count is not None:
            fields["tool_count"] = status.tool_count
        if status.error is not None:
            fields["error"] = status.error
        self.emit("mcp_server_status", **fields)


    def serve(self) -> None:
        opened = False
        while True:
            try:
                message = self.read_message()
            except ValueError as error:
                self.emit(
                    "fatal",
                    error={
                        "type": "ProtocolError",
                        "message": str(error),
                        "details": {},
                    },
                )
                raise SystemExit(1)

            if message is None:
                return
            if not opened or message["type"] == "open_session":
                if message["type"] == "list_sessions":
                    self._emit_sessions()
                    continue
                if message["type"] != "open_session":
                    self.emit(
                        "fatal",
                        error={
                            "type": "ProtocolError",
                            "message": "first message must be 'open_session'",
                            "details": {},
                        },
                    )
                    raise SystemExit(1)
                try:
                    self.open_session(message)
                except (SessionInUseError, ValueError, OSError) as error:
                    opened = self.host.sessions.session is not None
                    self.emit(**session_open_failed_message(message.get("session_id"), error))
                    continue
                opened = True
            elif message["type"] == "scene_task_start":
                self.run_scene_task(message)
            elif message["type"] == "provider_set":
                self._set_provider(message)
            elif message["type"] == "runtime_refresh":
                self._refresh_runtime()
            elif message["type"] == "list_sessions":
                self._emit_sessions()
            elif message["type"] == "list_input_scenes":
                try:
                    filenames = list_scene_inputs(self._input_directory)
                except (OSError, ValueError) as error:
                    self.emit("input_scenes_listed", filenames=[], error=str(error))
                else:
                    self.emit("input_scenes_listed", filenames=filenames)
            elif message["type"] == "load_session":
                self._emit_session_items()
            elif message["type"] == "session_snapshot":
                self._emit_session_snapshot(resumed=True)
            elif message["type"] == "settings_get":
                self._emit_settings(message)
            elif message["type"] == "settings_provider_save":
                self._save_provider_settings(message)
            elif message["type"] == "settings_agent_save":
                self._save_agent_settings(message)


    def open_session(self, message: dict[str, object]) -> None:
        session_id = message.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            raise ValueError("session_id must be a non-empty string or null")
        resumed = self.host.open_session(
            session_id=session_id,
            provider=message.get("provider"),
        )
        self._phase = "inactive"
        self._runtime_warnings = ()
        self._jobs.clear()
        self._checkpoint_turn = None
        self._emit_session_snapshot(resumed=resumed)

    def _emit_session_snapshot(self, *, resumed: bool) -> None:
        state = self.host.sessions
        self.emit(**session_ready_message(
            session_id=state.session.session_id,
            provider=state.provider_name,
            model=load_model_options(self.host.config_directory / "provider_config.json")[1][state.provider_name],
            resumed=resumed,
            message_count=len(state.session.items),
        ))
        self.emit(**self._runtime_snapshot())

    def _set_provider(self, message: dict[str, object]) -> None:
        provider = message.get("provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError("provider must be a non-empty string")
        changed, model = self.host.change_provider(provider)
        self.emit("provider_changed", provider=provider, model=model)
        if changed:
            self._emit_runtime_state("inactive")

    def _refresh_runtime(self) -> None:
        try:
            provider, model = self.host.refresh_runtime()
        except Exception as error:
            self.emit(
                "runtime_refresh_failed",
                error=runtime_failure_to_dict(runtime_error_info(error)),
            )
        else:
            self.emit("runtime_refreshed", provider=provider, model=model)

    def run_scene_task(self, message: dict[str, object]) -> None:
        turn_id = message["turn_id"]
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("scene_task_start requires a non-empty turn_id")
        self._checkpoint_turn = turn_id
        outcome = self.host.run_scene_task(
            turn_id, message.get("scene_input"), self._input_directory, self._output_directory,
        )
        self._jobs.clear()
        self._emit_runtime_state(
            "failed" if outcome.startup_failed else "idle",
            runtime_warnings=outcome.runtime_warnings,
        )
        status = outcome.task["status"]
        fields = {"turn_id": turn_id, "task": outcome.task}
        if status == "completed":
            fields["usage"] = usage_to_dict(outcome.usage)
        elif status in {"failed", "timed_out"}:
            fields["error"] = outcome.task["error"]
        self.emit(f"scene_task_{status}", **fields)

    def close(self) -> None:
        self._route_shutdown(notify_commands=False)
        self.host.close()
