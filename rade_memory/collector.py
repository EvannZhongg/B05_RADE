"""Explicit archival of a scene run's original evidence into domain memory."""

import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterable

from agent_core.session import JournalEvent, Session
from agent_core.time_utils import format_utc
from agent_core.workspace import Workspace

from .database import transaction
from .migrations import SCHEMA_VERSION, current_schema_version


TERMINAL = {"completed", "failed", "timed_out", "cancelled", "interrupted"}
STATUSES = TERMINAL | {"running"}


class CollectionConflict(ValueError):
    """An immutable source identity already names different archived evidence."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Journal timestamp must be timezone-aware")
    return format_utc(value)


def _document(run_id: str, kind: str, source_id: str, content: str, *,
              digest: str | None = None, source_path: str | None = None,
              metadata: dict[str, object] | None = None) -> tuple[object, ...]:
    identity = _json([run_id, kind, source_id])
    document_id = sha256(identity.encode("utf-8")).hexdigest()
    return (document_id, run_id, digest, kind, content,
            sha256(content.encode("utf-8")).hexdigest(), source_path,
            _json(metadata or {}), format_utc(datetime.now(timezone.utc)))


def _insert_document(db: sqlite3.Connection, row: tuple[object, ...]) -> bool:
    existing = db.execute("SELECT * FROM raw_documents WHERE document_id = ?", (row[0],)).fetchone()
    if existing is not None:
        fields = ("scene_run_id", "scene_digest", "document_type", "content", "content_hash",
                  "source_path", "metadata_json")
        if tuple(existing[field] for field in fields) != row[1:8]:
            raise CollectionConflict(f"raw document identity conflict: {row[0]}")
        return False
    db.execute("""INSERT INTO raw_documents
        (document_id, scene_run_id, scene_digest, document_type, content, content_hash,
         source_path, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", row)
    return True


def _task_events(events: tuple[JournalEvent, ...], task_id: str) -> list[JournalEvent]:
    return [event for event in events if event.event_type == "application_scene_task"
            and event.payload.get("task_id") == task_id]


def _run_values(session_id: str, task: dict[str, object], events: tuple[JournalEvent, ...]) -> tuple[object, ...]:
    task_id = task.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id is required for a scene run")
    history = _task_events(events, task_id)
    terminal_events = [event for event in history if event.payload.get("status") in TERMINAL]
    # A terminal Journal event is stronger evidence than an older in-memory running snapshot.
    if terminal_events and task.get("status") == "running":
        task = dict(terminal_events[-1].payload)
    status = task.get("status")
    if status not in STATUSES:
        raise ValueError(f"unsupported scene task status: {status}")
    starts = [event for event in history if event.payload.get("status") == "running"]
    finishes = [event for event in terminal_events if event.payload.get("status") == status]
    return (
        task_id, session_id, task.get("scene_digest"), task.get("filename"), task.get("mode"), status,
        task.get("baseline_experiment_id"), task.get("experiment_id"),
        task.get("baseline_expected_value"), task.get("expected_value"),
        _utc(starts[0].timestamp_utc) if starts else None,
        _utc(finishes[-1].timestamp_utc) if finishes else None,
        _json(task["error"]) if task.get("error") is not None else None,
    )


_RUN_COLUMNS = ("scene_run_id", "session_id", "scene_digest", "scene_filename", "mode", "status",
                "baseline_experiment_id", "selected_experiment_id", "baseline_expected_value",
                "final_expected_value", "started_at", "finished_at", "error_json")


def _upsert_run(db: sqlite3.Connection, values: tuple[object, ...]) -> None:
    existing = db.execute("SELECT * FROM scene_runs WHERE scene_run_id = ?", (values[0],)).fetchone()
    if existing is None:
        db.execute(f"INSERT INTO scene_runs ({', '.join(_RUN_COLUMNS)}) VALUES ({', '.join('?' for _ in values)})", values)
        return
    if existing["session_id"] != values[1]:
        raise CollectionConflict(f"scene run session conflict: {values[0]}")
    if existing["status"] in TERMINAL and values[5] == "running":
        return
    if existing["status"] in TERMINAL and values[5] != existing["status"]:
        raise CollectionConflict(f"scene run terminal status conflict: {values[0]}")
    merged = []
    for field, incoming in zip(_RUN_COLUMNS, values):
        old = existing[field]
        if old is not None and incoming is not None and old != incoming and field not in {"status"}:
            raise CollectionConflict(f"scene run field conflict: {values[0]}.{field}")
        merged.append(incoming if incoming is not None else old)
    if tuple(existing[field] for field in _RUN_COLUMNS) != tuple(merged):
        db.execute(f"UPDATE scene_runs SET {', '.join(f'{field} = ?' for field in _RUN_COLUMNS[1:])} "
                   "WHERE scene_run_id = ?", (*merged[1:], values[0]))


def _event_owner(event: JournalEvent, task_id: str, known_tasks: set[str]) -> str | None:
    if event.event_type == "application_scene_task":
        owner = event.payload.get("task_id")
        return owner if isinstance(owner, str) else None
    if event.event_type == "job_submitted":
        details = event.payload.get("details")
        owner = details.get("task_id") if isinstance(details, dict) else None
        if isinstance(owner, str):
            if event.turn_id is not None and event.turn_id != owner:
                raise CollectionConflict(f"Job task identity conflict: {event.event_id}")
            return owner
    return event.turn_id if event.turn_id in known_tasks else None


def _insert_events(db: sqlite3.Connection, session_id: str, task_id: str,
                   events: tuple[JournalEvent, ...]) -> int:
    known_tasks = {str(event.payload["task_id"]) for event in events
                   if event.event_type == "application_scene_task" and isinstance(event.payload.get("task_id"), str)}
    known_tasks.update(event.payload["details"]["task_id"] for event in events
                       if event.event_type == "job_submitted"
                       and isinstance(event.payload.get("details"), dict)
                       and isinstance(event.payload["details"].get("task_id"), str))
    known_tasks.add(task_id)
    count = 0
    for event in events:
        owner = _event_owner(event, task_id, known_tasks)
        if owner is not None and owner != task_id:
            continue
        row = (event.event_id, task_id if owner == task_id else None, session_id, event.seq,
               event.event_type, event.turn_id, event.tool_call_id, _json(event.payload),
               _utc(event.timestamp_utc))
        existing = db.execute("SELECT * FROM interaction_events WHERE event_id = ? OR "
                              "(session_id = ? AND sequence_no = ?)",
                              (event.event_id, session_id, event.seq)).fetchone()
        fields = ("event_id", "scene_run_id", "session_id", "sequence_no", "event_type", "turn_id",
                  "tool_call_id", "payload_json", "timestamp_utc")
        if existing is not None:
            if any(existing[field] != value for field, value in zip(fields, row)
                   if field != "scene_run_id"):
                raise CollectionConflict(f"Journal event identity conflict: {event.event_id}")
            if existing["scene_run_id"] is None and row[1] is not None:
                db.execute("UPDATE interaction_events SET scene_run_id = ? WHERE event_id = ?",
                           (row[1], event.event_id))
            elif existing["scene_run_id"] is not None and row[1] is not None and existing["scene_run_id"] != row[1]:
                raise CollectionConflict(f"Journal event ownership conflict: {event.event_id}")
            continue
        db.execute(f"INSERT INTO interaction_events ({', '.join(fields)}) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        count += 1
    return count


def _scene_document(task_id: str, task: dict[str, object], scene: object, files: Workspace) -> tuple[object, ...]:
    source_path = getattr(scene, "source_path")
    digest = getattr(scene, "digest")
    filename = getattr(scene, "filename")
    data = files.resolve_path(source_path).read_bytes()
    actual = sha256(data).hexdigest()
    if actual != digest or (task.get("scene_digest") is not None and task["scene_digest"] != digest):
        raise ValueError("scene snapshot SHA256 does not match SceneInput or task")
    return _document(task_id, "scene", source_path, data.decode("utf-8"), digest=digest,
                     source_path=source_path, metadata={"filename": filename, "digest": digest,
                                                        "source": "SceneInput.source_path"})


def _experiment_documents(task_id: str, digest: str | None, events: tuple[JournalEvent, ...],
                          files: Workspace | None) -> list[tuple[object, ...]]:
    documents = []
    for event in events:
        if event.turn_id != task_id or not event.event_type.startswith("job_") or event.payload.get("kind") != "experiment":
            continue
        job_id = event.payload.get("job_id")
        if not isinstance(job_id, str):
            continue
        documents.append(_document(task_id, "experiment", event.event_id, _json(event.payload), digest=digest,
                                   metadata={"source": "Session Journal", "event_id": event.event_id,
                                             "event_type": event.event_type, "experiment_id": job_id}))
        summary = event.payload.get("result_summary")
        if event.event_type != "job_completed" or not isinstance(summary, dict) or not summary.get("valid"):
            continue
        result_ref, allocation_digest = summary.get("result_ref"), summary.get("allocation_digest")
        if files is None or not isinstance(result_ref, str) or not isinstance(allocation_digest, str):
            continue
        data = files.resolve_path(result_ref).read_bytes()
        if sha256(data).hexdigest() != allocation_digest:
            raise ValueError(f"allocation SHA256 mismatch: {result_ref}")
        documents.append(_document(task_id, "experiment", f"allocation:{job_id}:{result_ref}",
                                   data.decode("utf-8"), digest=digest, source_path=result_ref,
                                   metadata={"source": "ExperimentRunner allocation", "experiment_id": job_id,
                                             "allocation_digest": allocation_digest}))
    return documents


def _reflection_documents(path: Path, session_id: str, task_id: str,
                          digest: str | None) -> list[tuple[object, ...]]:
    if not path.is_file():
        return []
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reflections'").fetchone() is None:
            return []
        records = db.execute("SELECT reflection_id, payload FROM reflections").fetchall()
    finally:
        db.close()
    documents = []
    for reflection_id, payload in records:
        data = json.loads(payload)
        source = data.get("source")
        if not isinstance(source, dict) or source.get("session_id") != session_id or source.get("task_id") != task_id:
            continue
        if digest is not None and source.get("scene_digest") != digest:
            raise CollectionConflict(f"reflection scene digest conflict: {reflection_id}")
        documents.append(_document(task_id, "reflection", reflection_id, payload, digest=digest,
                                   metadata={"source": "reflections.payload", "reflection_id": reflection_id,
                                             "status": data.get("status"),
                                             "reference_experiment_id": data.get("proposal", {}).get("reference_experiment_id"),
                                             "experiment_id": data.get("proposal", {}).get("experiment_id")}))
    return documents


def _outcome_documents(task_id: str, task: dict[str, object], digest: str | None,
                       output_directory: Path | None) -> list[tuple[object, ...]]:
    documents = []
    if task.get("status") == "completed" and task.get("output_path") is not None and output_directory is not None:
        output = Path(str(task["output_path"])).resolve()
        expected = (output_directory / str(task["mode"]) / str(task["filename"])).resolve()
        if output != expected:
            raise ValueError("result path is outside the expected output location")
        content = output.read_bytes().decode("utf-8")
        documents.append(_document(task_id, "final_result", str(output), content, digest=digest,
                                   source_path=str(output), metadata={"source": "task.output_path",
                                   "experiment_id": task.get("experiment_id"),
                                   "expected_value": task.get("expected_value")}))
    if task.get("error") is not None:
        documents.append(_document(task_id, "error", "task.error", _json(task["error"]), digest=digest,
                                   metadata={"source": "task.error"}))
    return documents


def collect_scene_run(database: Path, session: Session, task: dict[str, object], *,
                      events: Iterable[JournalEvent] | None = None, files: Workspace | None = None,
                      scene: object | None = None, reflections_database: Path | None = None,
                      output_directory: Path | None = None) -> dict[str, int]:
    """Archive one task and its authentic evidence; caller initializes the memory schema first.

    Pass an unrecovered Session or a snapshot loaded with ``recover=False``. Events from
    other scene tasks in the same Session are left for their own collection call.
    """
    if current_schema_version(database) != SCHEMA_VERSION:
        raise ValueError("domain-memory database must be initialized before collection")
    snapshot = tuple(event for event in (session.journal if events is None else events)
                     if event.event_type not in {"turn_recovered", "tool_recovered", "job_interrupted"})
    task_id_input = task.get("task_id")
    if isinstance(task_id_input, str) and task.get("status") == "running":
        terminal_events = [event for event in _task_events(snapshot, task_id_input)
                           if event.payload.get("status") in TERMINAL]
        if terminal_events:
            task = dict(terminal_events[-1].payload)
    values = _run_values(session.session_id, task, snapshot)
    task_id, digest = str(values[0]), values[2]
    documents = []
    if scene is not None:
        if files is None:
            raise ValueError("Workspace is required to read SceneInput.source_path")
        documents.append(_scene_document(task_id, task, scene, files))
    documents.extend(_experiment_documents(task_id, digest, snapshot, files))
    if reflections_database is not None:
        documents.extend(_reflection_documents(reflections_database, session.session_id, task_id, digest))
    documents.extend(_outcome_documents(task_id, task, digest, output_directory))
    with transaction(database) as db:
        _upsert_run(db, values)
        event_count = _insert_events(db, session.session_id, task_id, snapshot)
        document_count = sum(_insert_document(db, row) for row in documents)
    return {"events_inserted": event_count, "documents_inserted": document_count}
