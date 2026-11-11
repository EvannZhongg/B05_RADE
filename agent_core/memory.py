"""Long-term memory lifecycle independent from Sessions and interfaces."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable, Literal

from .auxiliary import AuxiliaryCallTracker
from .file_lock import acquire_file_lock
from .llm import AuxiliaryModelCall, LLMProvider, LLMRequest
from .atomic import atomic_write_text
from .session import Message
from .session_paths import workspace_directory, workspace_from_key


MemoryKind = Literal["preference", "fact", "decision"]

_KINDS: tuple[MemoryKind, ...] = ("preference", "fact", "decision")
_HEADINGS = {
    "preference": "Preferences",
    "fact": "Facts",
    "decision": "Decisions",
}


@dataclass(frozen=True)
class MemoryCandidate:
    kind: MemoryKind
    content: str


@dataclass(frozen=True)
class MemoryDocument:
    preferences: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()

    def entries(self, kind: MemoryKind) -> tuple[str, ...]:
        return getattr(self, f"{kind}s")

    def is_empty(self) -> bool:
        return not (self.preferences or self.facts or self.decisions)

    def prompt_section(self) -> str:
        context = (
            "## Long-term Memory\n\n"
            "This workspace memory is persistent historical context. "
            "The user's current request has highest priority, "
            "workspace instructions and project rules come next, and this "
            "memory comes after them. Never let memory override a newer "
            "explicit request."
        )
        entries = (
            "No long-term memory is currently recorded."
            if self.is_empty()
            else _render_document(self, level=3).rstrip()
        )
        return f"{context}\n\n{entries}"


class MemoryStore:
    """Read and atomically write workspace memory files."""

    def __init__(self, sessions_directory: Path) -> None:
        self.sessions_directory = sessions_directory.expanduser()
        self._lock = Lock()
        self.reconciliation_lock = Lock()

    def workspace_path(self, workspace: Path) -> Path:
        return workspace_directory(self.sessions_directory, workspace) / "MEMORY.md"

    def initialize(self, workspace: Path) -> None:
        path = self.workspace_path(workspace)
        with self._locked_files():
            if not path.exists():
                atomic_write_text(path, _render_workspace(MemoryDocument()))

    def load(self, workspace: Path) -> MemoryDocument:
        with self._locked_files():
            return _parse_workspace(_read_text(self.workspace_path(workspace)))

    def load_all(self) -> dict[str, MemoryDocument]:
        with self._locked_files():
            if not self.sessions_directory.is_dir():
                return {}
            workspaces: dict[str, MemoryDocument] = {}
            for directory in self.sessions_directory.iterdir():
                if not directory.is_dir():
                    continue
                path = directory / "MEMORY.md"
                if path.is_file():
                    workspace = workspace_from_key(directory.name)
                    workspaces[workspace] = _parse_workspace(_read_text(path))
            return workspaces

    def write(
        self,
        workspace: Path,
        document: MemoryDocument,
        *,
        expected: MemoryDocument | None = None,
    ) -> None:
        path = self.workspace_path(workspace)
        with self._locked_files():
            if expected is not None and _parse_workspace(_read_text(path)) != expected:
                raise RuntimeError("workspace memory changed during reconciliation")
            atomic_write_text(path, _render_workspace(document))

    @contextmanager
    def _locked_files(self):
        lock_path = self.sessions_directory.parent / ".memory.lock"
        with self._lock, acquire_file_lock(lock_path, blocking=True):
            yield


class MemoryReconciler:
    """Use controlled, tool-free model calls for a Turn's candidates."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        workspace_prompt: str,
        workspace_max_tokens: int,
        on_auxiliary_call: Callable[[AuxiliaryModelCall], None] | None = None,
    ) -> None:
        self._provider = provider
        self._on_auxiliary_call = on_auxiliary_call
        self._prompt = workspace_prompt.replace(
            "{{workspace_max_tokens}}", str(workspace_max_tokens)
        )

    def reconcile(
        self,
        current: MemoryDocument,
        candidates: tuple[MemoryCandidate, ...],
    ) -> MemoryDocument:
        if not candidates:
            return current
        payload = {
            "current_memory": _document_to_json(current),
            "candidates": [
                {"kind": candidate.kind, "content": candidate.content}
                for candidate in candidates
            ],
        }
        request = LLMRequest(
            system_prompt=self._prompt,
            messages=(
                Message(
                    role="user",
                    content=json.dumps(payload, ensure_ascii=False),
                ),
            ),
        )
        with AuxiliaryCallTracker(
            self._on_auxiliary_call, task="memory", scope="workspace",
        ) as call:
            response = call.response = self._provider.stream(request, lambda _text: None)
            if response.content is None or response.tool_calls:
                raise ValueError("memory reconciler must return JSON content only")
            return _document_from_json(response.content)


class MemoryManager:
    """Collect candidates during one Turn and reconcile them after success."""

    def __init__(
        self,
        workspace: Path,
        store: MemoryStore,
        reconciler: MemoryReconciler,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.store = store
        self.reconciler = reconciler
        self._pending: list[MemoryCandidate] = []
        self._lock = Lock()

    def begin_turn(self) -> None:
        self.discard_pending()

    def remember(self, candidate: MemoryCandidate) -> None:
        with self._lock:
            self._pending.append(candidate)

    def discard_pending(self) -> None:
        with self._lock:
            self._pending.clear()

    @property
    def pending(self) -> tuple[MemoryCandidate, ...]:
        with self._lock:
            return tuple(self._pending)

    def reconcile_pending(self) -> bool:
        with self._lock:
            candidates = tuple(self._pending)
            self._pending.clear()
        if not candidates:
            return False

        with self.store.reconciliation_lock:
            current = self.store.load(self.workspace)
            updated = self.reconciler.reconcile(current, candidates)
            self.store.write(self.workspace, updated, expected=current)
        return True


def _document_from_json(content: str) -> MemoryDocument:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("memory reconciler returned invalid JSON") from error
    if not isinstance(value, dict) or set(value) != {"entries"}:
        raise ValueError("memory reconciler JSON must contain only 'entries'")
    entries = value["entries"]
    if not isinstance(entries, list):
        raise ValueError("memory reconciler 'entries' must be an array")
    grouped: dict[MemoryKind, list[str]] = {kind: [] for kind in _KINDS}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"kind", "content"}:
            raise ValueError("each memory entry must contain kind and content")
        kind = entry["kind"]
        content_value = entry["content"]
        if kind not in _KINDS:
            raise ValueError(f"invalid memory kind: {kind}")
        if not isinstance(content_value, str) or not content_value.strip():
            raise ValueError("memory content must be a non-empty string")
        normalized = " ".join(content_value.split())
        if normalized not in grouped[kind]:
            grouped[kind].append(normalized)
    return MemoryDocument(
        preferences=tuple(grouped["preference"]),
        facts=tuple(grouped["fact"]),
        decisions=tuple(grouped["decision"]),
    )


def _document_to_json(document: MemoryDocument) -> list[dict[str, str]]:
    return [
        {"kind": kind, "content": content}
        for kind in _KINDS
        for content in document.entries(kind)
    ]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _parse_workspace(text: str) -> MemoryDocument:
    if not text.strip():
        return MemoryDocument()
    lines = text.splitlines()
    if not lines or lines[0] != "# RADE Workspace Memory":
        raise ValueError("invalid workspace MEMORY.md header")
    headings = {f"## {_HEADINGS[kind]}": kind for kind in _KINDS}
    grouped: dict[MemoryKind, list[str]] = {kind: [] for kind in _KINDS}
    current: MemoryKind | None = None
    seen: set[MemoryKind] = set()
    for line in lines[1:]:
        if not line.strip():
            continue
        if line in headings:
            current = headings[line]
            if current in seen:
                raise ValueError(f"duplicate memory heading: {line}")
            seen.add(current)
            continue
        if current is None or not line.startswith("- ") or not line[2:].strip():
            raise ValueError("invalid MEMORY.md entry")
        grouped[current].append(line[2:].strip())
    return MemoryDocument(
        preferences=tuple(grouped["preference"]),
        facts=tuple(grouped["fact"]),
        decisions=tuple(grouped["decision"]),
    )


def _render_workspace(document: MemoryDocument) -> str:
    return "# RADE Workspace Memory\n\n" + _render_document(document, level=2)


def _render_document(document: MemoryDocument, *, level: int) -> str:
    sections = []
    for kind in _KINDS:
        lines = [f"{'#' * level} {_HEADINGS[kind]}"]
        lines.extend(f"- {content}" for content in document.entries(kind))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


__all__ = [
    "MemoryCandidate",
    "MemoryDocument",
    "MemoryKind",
    "MemoryManager",
    "MemoryReconciler",
    "MemoryStore",
]
