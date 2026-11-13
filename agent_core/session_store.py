"""JSONL append-only runtime journal storage."""

import json
import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator, TextIO

from .atomic import atomic_write_text
from .content import FilePart, text_content
from .file_lock import acquire_file_lock
from .session import JournalEvent, JournalSink, Session, message_from_dict
from .session_paths import default_runs_directory, session_directory, session_log_path, validate_session_id
from .time_utils import format_utc as _format_utc, parse_utc as _parse_utc

SESSION_METADATA_FILENAME = "session.json"


class SessionInUseError(RuntimeError):
    """Another writer owns the Session journal."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session '{session_id}' is already in use by another runtime.")


class JsonlSessionStore:
    """Persist one durable JSON record for every runtime journal event."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = (directory or default_runs_directory()).expanduser().resolve()

    @property
    def directory(self) -> Path:
        return self._directory

    def session_directory(self, session_id: str) -> Path:
        return session_directory(self._directory, session_id)

    def has_journal(self, session_id: str) -> bool:
        with self._locked_session(session_id):
            return self._session_path(session_id).is_file()

    def delete_session(self, session_id: str) -> bool:
        with self._writer_lock(session_id), self._locked_session(session_id):
            directory = self.session_directory(session_id)
            if not directory.is_dir():
                return False
            shutil.rmtree(directory)
            return True

    def list_sessions(
        self, *, should_cancel: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        if should_cancel is not None and should_cancel():
            return []
        return [
            summary
            for _, _, summary in _scan_sessions(self._directory, should_cancel=should_cancel)
        ]

    def search_messages(
        self, query: str, *, limit: int, per_session_limit: int,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[dict[str, object]]:
        """Stream conversation text from Journals, discarding cancelled searches."""
        query = query.strip()
        if not query or limit <= 0 or per_session_limit <= 0:
            return []
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        matches: list[dict[str, object]] = []
        for summary in self.list_sessions(should_cancel=should_cancel):
            if should_cancel is not None and should_cancel():
                return []
            session_id = summary["session_id"]
            path = self._session_path(session_id)
            session_matches: list[dict[str, object]] = []
            with self._locked_session(session_id):
                if not path.is_file():
                    continue
                with path.open(encoding="utf-8") as file:
                    item_index = -1
                    for record in _read_journal_records(file, should_cancel=should_cancel):
                        if record["event_type"] != "message_appended":
                            continue
                        data = record["payload"].get("message")
                        if not isinstance(data, dict):
                            continue
                        item_index += 1
                        if data["role"] not in {"user", "assistant"}:
                            continue
                        message = message_from_dict(data)
                        if message.origin != "conversation":
                            continue
                        text = text_content(message.content)
                        match = pattern.search(text) if text else None
                        if match is None:
                            continue
                        if len(session_matches) == per_session_limit:
                            for result in session_matches:
                                result["has_more_in_session"] = True
                            break
                        session_matches.append({
                            **summary,
                            "item_index": item_index,
                            "role": message.role,
                            "has_more_in_session": False,
                            "text": text,
                            "match_start": match.start(),
                            "match_end": match.end(),
                            "timestamp_utc": (
                                _format_utc(message.timestamp_utc)
                                if message.timestamp_utc is not None else None
                            ),
                        })
            if should_cancel is not None and should_cancel():
                return []
            matches.extend(session_matches)
            if len(matches) >= limit:
                return matches[:limit]
        return matches

    def load(self, session_id: str, *, recover: bool = True) -> Session:
        with self._locked_session(session_id):
            return self._load_unlocked(session_id, recover=recover)

    def _load_unlocked(self, session_id: str, *, recover: bool) -> Session:
        path = self._session_path(session_id)
        session = Session(session_id=session_id)
        if not path.is_file():
            return session
        with path.open(encoding="utf-8") as file:
            for record in _read_journal_records(file):
                event = _event_from_dict(record)
                session.apply_event(event)
                session.journal.append(event)
        if recover:
            session.recover()
        return session

    def provider_for(self, session_id: str) -> str | None:
        with self._locked_session(session_id):
            value = _session_metadata(self.session_directory(session_id)).get("provider")
            return value if isinstance(value, str) and value else None

    def set_provider(self, session_id: str, provider: str) -> None:
        with self._locked_session(session_id):
            directory = self.session_directory(session_id)
            directory.mkdir(parents=True, exist_ok=True)
            metadata = _session_metadata(directory) or _new_session_metadata()
            metadata["provider"] = provider
            _write_session_metadata(directory, metadata)

    def append_events(self, session_id: str, events: Iterable[JournalEvent]) -> None:
        events = tuple(events)
        if not events:
            return
        with self._writer_lock(session_id), self._locked_session(session_id):
            self._append_events_unlocked(session_id, events)

    @contextmanager
    def open_journal(self, session_id: str) -> Iterator[JournalSink]:
        """Own the active journal until its Runtime releases the writer."""
        with self._writer_lock(session_id):
            with self._locked_session(session_id):
                path = self._prepare_session_directory(session_id)
                file = _open_journal_file(path)
            with file:
                with self._locked_session(session_id):
                    _prepare_journal_append(file)

                def append(events: tuple[JournalEvent, ...]) -> None:
                    with self._locked_session(session_id):
                        _write_journal_events(file, events)
                        _update_session_metadata(path.parent, events)

                yield append

    def _prepare_session_directory(self, session_id: str) -> Path:
        path = self._session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not (path.parent / SESSION_METADATA_FILENAME).exists():
            _write_session_metadata(path.parent, _new_session_metadata())
        return path

    def _append_events_unlocked(self, session_id: str, events: tuple[JournalEvent, ...]) -> None:
        path = self._prepare_session_directory(session_id)
        with _open_journal_file(path) as file:
            _prepare_journal_append(file)
            _write_journal_events(file, events)
        _update_session_metadata(path.parent, events)

    @contextmanager
    def _locked_session(self, session_id: str) -> Iterator[None]:
        validate_session_id(session_id)
        lock_path = self._directory / ".locks" / f"{session_id}.lock"
        with acquire_file_lock(lock_path, blocking=True):
            yield

    @contextmanager
    def _writer_lock(self, session_id: str) -> Iterator[None]:
        validate_session_id(session_id)
        path = self._directory / ".locks" / f"{session_id}.writer.lock"
        try:
            lock = acquire_file_lock(path)
        except BlockingIOError as error:
            raise SessionInUseError(session_id) from error
        with lock:
            yield

    def _session_path(self, session_id: str) -> Path:
        return session_log_path(self._directory, session_id)



def _open_journal_file(path: Path) -> BinaryIO:
    if os.name != "nt":
        return path.open("a+b")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    # GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ, OPEN_ALWAYS.
    # Do not share writes/deletes or allow the handle to be inherited.
    handle = create_file(str(path), 0xC0000000, 0x1, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        if error.winerror == 32:  # ERROR_SHARING_VIOLATION
            raise SessionInUseError(path.stem) from error
        raise error
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDWR | os.O_BINARY | os.O_APPEND | os.O_NOINHERIT,
        )
    except BaseException:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
        raise
    try:
        return os.fdopen(descriptor, "a+b")
    except BaseException:
        os.close(descriptor)
        raise


def _write_journal_events(file: BinaryIO, events: Iterable[JournalEvent]) -> None:
    for event in events:
        record = json.dumps(_event_to_dict(event), ensure_ascii=False)
        file.write(record.encode("utf-8") + b"\n")
    file.flush()
    os.fsync(file.fileno())


def _event_to_dict(event: JournalEvent) -> dict[str, object]:
    return {
        "seq": event.seq,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "turn_id": event.turn_id,
        "timestamp_utc": _format_utc(event.timestamp_utc),
        "tool_call_id": event.tool_call_id,
        "payload": event.payload,
    }


def _prepare_journal_append(file: BinaryIO) -> None:
    end = file.seek(0, os.SEEK_END)
    if not end:
        return
    file.seek(end - 1)
    if file.read(1) == b"\n":
        return

    cursor = end
    start = 0
    while cursor:
        chunk_start = max(0, cursor - 4096)
        file.seek(chunk_start)
        chunk = file.read(cursor - chunk_start)
        newline = chunk.rfind(b"\n")
        if newline != -1:
            start = chunk_start + newline + 1
            break
        cursor = chunk_start

    file.seek(start)
    tail = file.read(end - start)
    try:
        json.loads(tail)
    except json.JSONDecodeError:
        # Only an unfinished final record can be discarded during replay.
        file.truncate(start)
    else:
        file.write(b"\n")
    file.seek(0, os.SEEK_END)


def _read_journal_records(
    file: TextIO, *, should_cancel: Callable[[], bool] | None = None,
) -> Iterator[dict[str, object]]:
    for line in file:
        if should_cancel is not None and should_cancel():
            return
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # An interrupted append can leave an incomplete final record.
            if not line.endswith("\n"):
                return
            raise
        yield record


def _event_from_dict(data: dict[str, object]) -> JournalEvent:
    return JournalEvent(
        seq=int(data["seq"]),
        event_id=str(data["event_id"]),
        event_type=str(data["event_type"]),
        turn_id=(
            data.get("turn_id")
            if isinstance(data.get("turn_id"), str)
            else None
        ),
        timestamp_utc=_parse_utc(str(data["timestamp_utc"])),
        tool_call_id=(
            data.get("tool_call_id")
            if isinstance(data.get("tool_call_id"), str)
            else None
        ),
        payload=dict(data.get("payload") or {}),
    )


def _session_metadata(directory: Path) -> dict[str, object]:
    try:
        data = json.loads(
            (directory / SESSION_METADATA_FILENAME).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("session metadata must be an object")
    return data


def _write_session_metadata(
    directory: Path,
    metadata: dict[str, object],
) -> None:
    atomic_write_text(
        directory / SESSION_METADATA_FILENAME,
        json.dumps(metadata, ensure_ascii=False),
    )


def _new_session_metadata() -> dict[str, object]:
    return {"has_activity": False}


def _update_session_metadata(
    directory: Path, events: tuple[JournalEvent, ...],
) -> None:
    metadata = _session_metadata(directory)
    if _record_session_activity(metadata, events):
        _write_session_metadata(directory, metadata)


def _record_session_activity(
    metadata: dict[str, object], events: Iterable[JournalEvent],
) -> bool:
    changed = False
    for event in events:
        if "started_at" in metadata and "input_preview" in metadata:
            break
        if event.event_type == "plan_updated":
            continue
        if "started_at" not in metadata:
            metadata.update(has_activity=True, started_at=_format_utc(event.timestamp_utc))
            changed = True
        if "input_preview" in metadata or event.event_type != "message_appended":
            continue
        data = event.payload.get("message")
        if not isinstance(data, dict) or data.get("role") != "user":
            continue
        message = message_from_dict(data)
        if message.origin != "conversation":
            continue
        text = " ".join((text_content(message.content) or "").split())
        metadata["input_preview"] = text[:80] or next(
            (
                part.filename
                for part in message.parts
                if isinstance(part, FilePart) and part.filename
            ),
            None,
        )
        changed = True
    return changed


def _scan_sessions(
    directory: Path, *, should_cancel: Callable[[], bool] | None = None,
) -> list[tuple[int, str, dict[str, object]]]:
    """List summaries from metadata; journal contents are never opened."""
    if not directory.is_dir():
        return []
    found = []
    for session_dir in directory.iterdir():
        if should_cancel is not None and should_cancel():
            return []
        if session_dir.resolve() != session_dir:
            continue
        path = session_dir / f"{session_dir.name}.jsonl"
        if not path.is_file():
            continue
        metadata = _session_metadata(session_dir)
        if metadata.get("has_activity"):
            try:
                updated = path.stat().st_mtime_ns
            except FileNotFoundError:
                continue
            found.append((updated, session_dir.name, _session_summary(session_dir.name, metadata)))
    found.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return found


def _session_summary(session_id: str, metadata: dict[str, object]) -> dict[str, object]:
    return {
        "session_id": session_id,
        "input_preview": metadata.get("input_preview"),
        "started_at": metadata.get("started_at"),
    }
