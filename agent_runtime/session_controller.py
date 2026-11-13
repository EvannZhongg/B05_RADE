"""Session binding and provider selection."""

from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator
from agent_core import JsonlSessionStore, Session, Workspace, PlanManager
from .config import load_model_options
from .settings import EnvironmentReloader
from .callbacks import RuntimeCallbacks


class SessionRuntimeController:
    def __init__(self, config_directory: Path, environment: EnvironmentReloader,
                 callbacks: RuntimeCallbacks) -> None:
        self._config_path = config_directory / "provider_config.json"
        self._environment = environment
        self.callbacks = callbacks
        self.session: Session | None = None
        self.store = JsonlSessionStore(config_directory / "runs")
        self.files: Workspace | None = None
        self.provider_name: str | None = None
        self.plan: PlanManager | None = None
        self._journal = ExitStack()

    def close(self) -> None:
        if self.session is not None:
            self.session.attach_journal_sink(None)
        self._journal.close()

    @contextmanager
    def open_session(self, session_id: str | None = None,
                     provider: str | None = None) -> Iterator[bool]:
        """Prepare the target writer, then replace the binding after yielding."""
        config_path = self._config_path
        self._environment.reload()
        store = self.store
        default_provider, models = load_model_options(config_path)
        stored_provider = (
            store.provider_for(str(session_id))
            if isinstance(session_id, str) and session_id
            else None
        )
        main_provider_name = (
            provider
            if isinstance(provider, str) and provider
            else stored_provider or default_provider
        )
        if main_provider_name not in models:
            raise ValueError(f"provider '{main_provider_name}' is not configured")

        resumed = session_id is not None and store.has_journal(session_id)
        current_session = self.session
        reuse_writer = current_session is not None and session_id == current_session.session_id
        # Keep the current writer and execution state until the target is ready.
        # Reopening the current Session reuses the writer we already own.
        with ExitStack() as journal:
            if reuse_writer:
                session = current_session
            else:
                session = Session(session_id) if session_id is not None else Session()
                opening_id = session.session_id
                sink = journal.enter_context(store.open_journal(opening_id))
                if resumed:
                    session = store.load(opening_id, recover=False)
                session.attach_journal_sink(sink)
                session.recover()
            files_path = store.session_directory(session.session_id) / "files"
            if files_path.resolve() != files_path:
                raise ValueError("Invalid run files directory")
            files_path.mkdir(exist_ok=True)
            files = Workspace(files_path)
            store.set_provider(session.session_id, main_provider_name)
            yield resumed
            if not reuse_writer:
                self.close()
                self._journal = journal.pop_all()
            self.session = session
            self.files = files
        self.plan = PlanManager(
            self.session,
            self.callbacks.on_plan,
        )

        self.provider_name = main_provider_name

    def change_provider(self, provider: str) -> None:
        assert self.session is not None
        self.store.set_provider(self.session.session_id, provider)
        self.provider_name = provider
