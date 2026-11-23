"""Original-evidence collection from real RADE session and scene objects."""

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256

import pytest

from agent_core import Session, Workspace
from agent_runtime.scene_input import prepare_scene_input
from rade_memory.collector import CollectionConflict, collect_scene_run
from rade_memory.migrations import current_schema_version, initialize_database
from rade_tasks.knowledge import KnowledgeService
from test_knowledge import comparison


def _task(task_id="task-1", **extra):
    return {"task_id": task_id, "filename": "scene1_1.json", "mode": "norethink",
            "status": "running", **extra}


def _db(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    initialize_database(path)
    return path


def _rows(path, table):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return db.execute(f"SELECT * FROM {table}").fetchall()


def _scene(tmp_path):
    inputs = tmp_path / "input"
    inputs.mkdir()
    source = inputs / "scene1_1.json"
    original = '{"P":[[0.8,0.7],[1,0]],"v":[10,10]}\n'
    source.write_bytes(original.encode("utf-8"))
    root = tmp_path / "files"
    root.mkdir()
    files = Workspace(root)
    return prepare_scene_input(inputs, source.name, files), files, original


def test_journal_identity_payload_time_and_idempotence(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    task = _task()
    session.record_application_event("scene_task", task.copy())
    session.add_item("user", "hello")
    event = session.journal[1]
    first = collect_scene_run(path, session, task)
    second = collect_scene_run(path, session, task)
    rows = _rows(path, "interaction_events")
    assert first == {"events_inserted": 2, "documents_inserted": 0}
    assert second == {"events_inserted": 0, "documents_inserted": 0}
    assert rows[1]["event_id"] == event.event_id
    assert rows[1]["sequence_no"] == event.seq
    assert json.loads(rows[1]["payload_json"]) == event.payload
    assert rows[1]["timestamp_utc"].endswith("Z")
    assert _rows(path, "scene_runs")[0]["started_at"] == rows[0]["timestamp_utc"]


def test_event_id_and_sequence_conflicts_rollback(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    task = _task()
    session.record_application_event("scene_task", task.copy())
    collect_scene_run(path, session, task)
    original = session.journal[0]
    changed = replace(original, payload={**original.payload, "filename": "changed.json"})
    with pytest.raises(CollectionConflict):
        collect_scene_run(path, session, task, events=[changed])
    same_sequence = replace(original, event_id="another-id")
    with pytest.raises(CollectionConflict):
        collect_scene_run(path, session, task, events=[same_sequence])
    assert len(_rows(path, "interaction_events")) == 1


def test_two_tasks_and_unowned_session_events(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    first, second = _task("a"), _task("b")
    session.record_application_event("scene_task", first.copy())
    session.job_submitted("job-a", "experiment", "a", details={"task_id": "a"})
    session.add_item("user", "session message")
    session.record_application_event("scene_task", second.copy())
    session.job_submitted("job-b", "experiment", "b", details={"task_id": "b"})
    collect_scene_run(path, session, first)
    collect_scene_run(path, session, second)
    events = {row["event_id"]: row for row in _rows(path, "interaction_events")}
    assert len(events) == 5
    assert [events[event.event_id]["scene_run_id"] for event in session.journal] == ["a", "a", None, "b", "b"]
    assert {row["scene_run_id"] for row in _rows(path, "scene_runs")} == {"a", "b"}


def test_partial_journal_uses_experiment_task_id_before_task_start(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    first, second = _task("a"), _task("b")
    session.record_application_event("scene_task", first.copy())
    session.job_submitted("job-b", "experiment", "b", details={"task_id": "b"})
    partial = tuple(session.journal)
    result_a = collect_scene_run(path, session, first, events=partial)
    assert result_a == {"events_inserted": 1, "documents_inserted": 0}
    assert len(_rows(path, "interaction_events")) == 1
    session.record_application_event("scene_task", second.copy())
    collect_scene_run(path, session, second)
    events = {row["event_id"]: row for row in _rows(path, "interaction_events")}
    assert events[partial[1].event_id]["scene_run_id"] == "b"
    assert len(events) == 3


def test_later_task_start_promotes_matching_unowned_event(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    first, second = _task("a"), _task("b")
    session.record_application_event("scene_task", first.copy())
    session.job_started("job-b", "experiment", "b")
    pending = session.journal[1]
    collect_scene_run(path, session, first)
    before = {row["event_id"]: row for row in _rows(path, "interaction_events")}
    assert before[pending.event_id]["scene_run_id"] is None
    session.record_application_event("scene_task", second.copy())
    collect_scene_run(path, session, second)
    after = {row["event_id"]: row for row in _rows(path, "interaction_events")}
    assert after[pending.event_id]["scene_run_id"] == "b"
    assert after[pending.event_id]["payload_json"] == before[pending.event_id]["payload_json"]
    assert after[pending.event_id]["timestamp_utc"] == before[pending.event_id]["timestamp_utc"]
    assert len(after) == 3


def test_promotion_rejects_changed_original_event(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    first, second = _task("a"), _task("b")
    session.record_application_event("scene_task", first.copy())
    session.job_started("job-b", "experiment", "b")
    collect_scene_run(path, session, first)
    session.record_application_event("scene_task", second.copy())
    altered = replace(session.journal[1], payload={**session.journal[1].payload, "status": "completed"})
    with pytest.raises(CollectionConflict, match="identity conflict"):
        collect_scene_run(path, session, second, events=[altered, session.journal[2]])
    stored = {row["event_id"]: row for row in _rows(path, "interaction_events")}
    assert stored[altered.event_id]["scene_run_id"] is None
    assert {row["scene_run_id"] for row in _rows(path, "scene_runs")} == {"a"}


def test_owned_event_cannot_be_reassigned_to_another_task(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    first, second = _task("a"), _task("b")
    session.record_application_event("scene_task", first.copy())
    session.record_application_event("scene_task", second.copy())
    collect_scene_run(path, session, first)
    collect_scene_run(path, session, second)
    event_id = session.journal[1].event_id
    with sqlite3.connect(path) as db:
        db.execute("UPDATE interaction_events SET scene_run_id = 'a' WHERE event_id = ?", (event_id,))
    with pytest.raises(CollectionConflict, match="ownership conflict"):
        collect_scene_run(path, session, second)
    assert {row["event_id"]: row for row in _rows(path, "interaction_events")}[event_id]["scene_run_id"] == "a"


def test_two_tasks_repeated_collection_keeps_counts_stable(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    first, second = _task("a"), _task("b")
    session.record_application_event("scene_task", first.copy())
    session.job_submitted("job-a", "experiment", "a", details={"task_id": "a"})
    session.record_application_event("scene_task", second.copy())
    session.job_submitted("job-b", "experiment", "b", details={"task_id": "b"})
    for _ in range(2):
        collect_scene_run(path, session, first)
        collect_scene_run(path, session, second)
    assert len(_rows(path, "scene_runs")) == 2
    assert len(_rows(path, "interaction_events")) == 4
    assert len(_rows(path, "raw_documents")) == 2


def test_job_submission_rejects_conflicting_task_ids(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    first = _task("a")
    session.record_application_event("scene_task", first.copy())
    session.job_submitted("job-b", "experiment", "a", details={"task_id": "b"})
    with pytest.raises(CollectionConflict, match="Job task identity conflict"):
        collect_scene_run(path, session, first)
    assert _rows(path, "scene_runs") == []
    assert _rows(path, "interaction_events") == []
    assert _rows(path, "raw_documents") == []


def test_promotion_rolls_back_when_later_event_conflicts(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    first, second = _task("a"), _task("b")
    session.record_application_event("scene_task", first.copy())
    first_event = session.journal[0]
    session.job_started("job-b", "experiment", "b")
    pending = session.journal[1]
    collect_scene_run(path, session, first)
    assert {row["event_id"]: row for row in _rows(path, "interaction_events")}[pending.event_id]["scene_run_id"] is None

    session.record_application_event("scene_task", second.copy())
    second_event = session.journal[2]
    session.job_submitted("job-c", "experiment", "b", details={"task_id": "b"})
    collision = replace(session.journal[3], event_id=first_event.event_id)
    with pytest.raises(CollectionConflict, match="Journal event identity conflict"):
        collect_scene_run(path, session, second,
                          events=[first_event, pending, second_event, collision])

    events = {row["event_id"]: row for row in _rows(path, "interaction_events")}
    assert events[pending.event_id]["scene_run_id"] is None
    assert first_event.event_id in events
    assert second_event.event_id not in events
    assert {row["scene_run_id"] for row in _rows(path, "scene_runs")} == {"a"}
    assert _rows(path, "raw_documents") == []


def test_scene_original_hash_and_workspace_safety(tmp_path):
    path = _db(tmp_path)
    scene, files, original = _scene(tmp_path)
    session = Session(session_id="s")
    task = _task(scene_digest=scene.digest)
    collect_scene_run(path, session, task, scene=scene, files=files)
    doc = _rows(path, "raw_documents")[0]
    assert doc["content"] == original
    assert doc["content_hash"] == sha256(original.encode()).hexdigest()
    assert doc["source_path"] == scene.source_path
    assert doc["scene_digest"] == scene.digest
    with pytest.raises(ValueError, match="workspace path"):
        collect_scene_run(path, session, task, scene=replace(scene, source_path="../outside.json"), files=files)


def test_scene_hash_mismatch_rejected_without_partial_writes(tmp_path):
    path = _db(tmp_path)
    scene, files, _ = _scene(tmp_path)
    files.resolve_path(scene.source_path).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        collect_scene_run(path, Session(session_id="s"), _task(scene_digest=scene.digest), scene=scene, files=files)
    assert not _rows(path, "scene_runs")


def test_experiment_submitted_completed_failed_and_allocation(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    task = _task()
    session.record_application_event("scene_task", task.copy())
    root = tmp_path / "files"
    root.mkdir()
    files = Workspace(root)
    allocation = b'{"allocation":[1,0]}'
    result_ref = "experiments/x/allocation.json"
    files.resolve_path(result_ref).parent.mkdir(parents=True)
    files.resolve_path(result_ref).write_bytes(allocation)
    session.job_submitted("good", "experiment", "task-1", details={"strategy_id": "baseline", "task_id": "task-1"})
    session.job_finished("good", "experiment", "completed", turn_id="task-1", elapsed_seconds=1,
                         result_summary={"status": "completed", "valid": True,
                                         "metrics": {"expected_value": 10}, "result_ref": result_ref,
                                         "allocation_digest": sha256(allocation).hexdigest()}, progress=None)
    session.job_submitted("bad", "experiment", "task-1", details={"task_id": "task-1"})
    session.job_finished("bad", "experiment", "failed", turn_id="task-1", elapsed_seconds=0,
                         result_summary=None, progress=None, error="solver failed")
    collect_scene_run(path, session, task, files=files)
    docs = _rows(path, "raw_documents")
    assert len(docs) == 5
    assert any(doc["content"] == allocation.decode() for doc in docs)
    assert any(json.loads(doc["content"]).get("error") == "solver failed" for doc in docs if doc["source_path"] is None)
    assert _rows(path, "scene_runs")[0]["final_expected_value"] is None


def test_allocation_hash_mismatch_rejected(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    session.job_finished("x", "experiment", "completed", turn_id="task-1", elapsed_seconds=1,
                         result_summary={"valid": True, "result_ref": "bad.json",
                                         "allocation_digest": "0" * 64}, progress=None)
    root = tmp_path / "files"
    root.mkdir()
    files = Workspace(root)
    files.resolve_path("bad.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256"):
        collect_scene_run(path, session, _task(), files=files)
    assert not _rows(path, "scene_runs")


def test_reflection_payload_and_status_are_read_only(tmp_path):
    path = _db(tmp_path)
    proposal, source, scene, records = comparison((10, 10), "s")
    stored = KnowledgeService(path).propose(proposal, source, scene, records.__getitem__)
    session = Session(session_id="s")
    collect_scene_run(path, session, _task(source.task_id, scene_digest=source.scene_digest),
                      reflections_database=path)
    doc = _rows(path, "raw_documents")[0]
    assert doc["document_type"] == "reflection"
    assert json.loads(doc["content"])["reflection_id"] == stored["reflection_id"]
    assert json.loads(doc["metadata_json"])["status"] == stored["status"]
    assert json.loads(doc["content"])["source"]["task_id"] == source.task_id
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM reflections").fetchone()[0] == 1


def test_missing_reflection_table_is_not_created(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    session.record_application_event("scene_task", _task().copy())
    session.job_submitted("baseline", "experiment", "task-1", details={"task_id": "task-1"})
    collect_scene_run(path, session, _task(), reflections_database=path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name = 'reflections'").fetchone() is None
    assert len(_rows(path, "raw_documents")) == 1


def test_rejected_reflection_is_archived_without_promotion(tmp_path):
    path = _db(tmp_path)
    proposal, source, scene, records = comparison((10, 9), "s")
    rejected = KnowledgeService(path).propose(
        replace(proposal, expected_value_effect="regressed"), source, scene, records.__getitem__)
    assert rejected["status"] == "rejected"
    collect_scene_run(path, Session(session_id="s"), _task(source.task_id, scene_digest=source.scene_digest),
                      reflections_database=path)
    doc = _rows(path, "raw_documents")[0]
    assert json.loads(doc["content"])["status"] == "rejected"
    assert json.loads(doc["metadata_json"])["status"] == "rejected"


def test_final_result_error_and_terminal_status(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    output = tmp_path / "output"
    result = output / "norethink" / "scene1_1.json"
    result.parent.mkdir(parents=True)
    result.write_text('{"allocation":[0,1]}', encoding="utf-8")
    finished = _task(status="completed", output_path=str(result), experiment_id="e",
                     expected_value=42, baseline_experiment_id="e", baseline_expected_value=42)
    session.record_application_event("scene_task", _task().copy())
    session.record_application_event("scene_task", finished.copy())
    collect_scene_run(path, session, finished, output_directory=output)
    run = _rows(path, "scene_runs")[0]
    assert run["status"] == "completed" and run["finished_at"] is not None
    assert run["selected_experiment_id"] == "e" and run["final_expected_value"] == 42
    assert _rows(path, "raw_documents")[0]["content"] == result.read_text(encoding="utf-8")
    failed = _task("failed", status="failed", error={"type": "ValueError", "message": "bad"})
    session.record_application_event("scene_task", failed.copy())
    collect_scene_run(path, session, failed)
    assert any(row["document_type"] == "error" and json.loads(row["content"])["message"] == "bad"
               for row in _rows(path, "raw_documents"))


def test_stale_running_snapshot_cannot_replace_terminal(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    running = _task()
    completed = _task(status="completed", experiment_id="x", expected_value=1)
    session.record_application_event("scene_task", running.copy())
    session.record_application_event("scene_task", completed.copy())
    collect_scene_run(path, session, completed)
    collect_scene_run(path, session, running, events=[session.journal[0]])
    assert _rows(path, "scene_runs")[0]["status"] == "completed"


def test_transaction_rolls_back_run_if_event_conflicts(tmp_path):
    path = _db(tmp_path)
    original = Session(session_id="s")
    original.record_application_event("scene_task", _task("a").copy())
    collect_scene_run(path, original, _task("a"))
    other = Session(session_id="s")
    other.record_application_event("scene_task", _task("b").copy())
    collision = replace(other.journal[0], event_id=original.journal[0].event_id, seq=2)
    with pytest.raises(CollectionConflict):
        collect_scene_run(path, other, _task("b"), events=[collision])
    assert {row["scene_run_id"] for row in _rows(path, "scene_runs")} == {"a"}


def test_database_reopens_and_import_has_no_creation_side_effect(tmp_path):
    path = tmp_path / "missing.sqlite3"
    assert current_schema_version(path) is None
    assert not path.exists()
    with pytest.raises(ValueError, match="initialized"):
        collect_scene_run(path, Session(session_id="s"), _task())
    assert not path.exists()
    initialize_database(path)
    collect_scene_run(path, Session(session_id="s"), _task())
    assert _rows(path, "scene_runs")[0]["session_id"] == "s"


def test_recovered_events_are_not_misrepresented_as_original(tmp_path):
    path = _db(tmp_path)
    session = Session(session_id="s")
    session.begin_turn("task-1")
    session.recover()
    collect_scene_run(path, session, _task())
    assert all(row["event_type"] != "turn_recovered" for row in _rows(path, "interaction_events"))
