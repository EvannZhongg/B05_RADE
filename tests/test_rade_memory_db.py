"""Database foundation and migration tests for RADE domain memory."""

import json
import sqlite3
from contextlib import closing
from hashlib import sha256

import pytest

from rade_memory.database import connect_database, transaction
from rade_memory.migrations import current_schema_version, initialize_database
from rade_tasks.knowledge import KnowledgeService
from test_knowledge import comparison


NEW_TABLES = {
    "scene_runs", "interaction_events", "raw_documents", "graph_entities",
    "graph_relations", "knowledge_evidence", "memories", "memory_versions",
    "summary_runs", "schema_version",
}
NOW = "2026-01-01T00:00:00Z"


def _tables(db):
    return {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _insert_run(db):
    db.execute(
        "INSERT INTO scene_runs (scene_run_id, session_id, mode, status) VALUES (?, ?, ?, ?)",
        ("run-1", "session-1", "rethink", "completed"),
    )


def _insert_memory(db):
    db.execute(
        """INSERT INTO memories (
            memory_id, memory_type, source_scene_run_id, title,
            scenario_features_json, applicable_conditions_json, status,
            summary_method, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("memory-1", "strategy", "run-1", "Observed strategy effect",
         "{}", "{}", "candidate", "reflection_derived", NOW, NOW),
    )


def test_empty_database_initializes_only_new_tables(tmp_path):
    path = tmp_path / "nested" / "knowledge.sqlite3"
    assert current_schema_version(path) is None
    assert not path.exists()

    initialize_database(path)

    assert current_schema_version(path) == 1
    with closing(connect_database(path)) as db:
        assert NEW_TABLES <= _tables(db)
        assert "reflections" not in _tables(db)
        indexes = {row["name"] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )}
        assert {"interaction_events_run", "raw_documents_run", "graph_relations_source",
                "memories_status_created"} <= indexes
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_initialization_is_idempotent_and_preserves_data(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    initialize_database(path)
    with transaction(path) as db:
        _insert_run(db)

    initialize_database(path)

    with closing(connect_database(path)) as db:
        assert db.execute("SELECT session_id FROM scene_runs WHERE scene_run_id = 'run-1'").fetchone()[0] == "session-1"
        assert db.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_existing_reflections_survive_new_schema(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    service = KnowledgeService(path)
    proposal, source, scene, records = comparison((10, 10), "legacy")
    reflection = service.propose(proposal, source, scene, records.__getitem__)
    knowledge = service.explain(reflection["knowledge_id"])
    query = service.query(scene)

    initialize_database(path)
    initialize_database(path)

    with closing(connect_database(path)) as db:
        assert NEW_TABLES <= _tables(db)
        assert {"reflections", "knowledge_revisions"} <= _tables(db)
        assert db.execute("SELECT COUNT(*) FROM reflections").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM knowledge_revisions").fetchone()[0] == 1
    assert service.explain(reflection["reflection_id"]) == reflection
    assert service.explain(reflection["knowledge_id"]) == knowledge
    assert service.query(scene) == query


def test_four_memory_layers_can_store_and_read_related_records(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    initialize_database(path)
    content = '{"P":[[0.8]],"v":[10]}'
    with transaction(path) as db:
        _insert_run(db)
        db.execute(
            """INSERT INTO interaction_events
            (event_id, scene_run_id, session_id, sequence_no, event_type, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            ("event-1", "run-1", "session-1", 1, "tool_completed", '{"ok":true}'),
        )
        db.execute(
            """INSERT INTO interaction_events
            (event_id, session_id, sequence_no, event_type, payload_json)
            VALUES (?, ?, ?, ?, ?)""",
            ("event-outside", "session-1", 2, "message_appended", "{}"),
        )
        db.execute(
            """INSERT INTO raw_documents
            (document_id, scene_run_id, document_type, content, content_hash, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("document-1", "run-1", "scene", content, sha256(content.encode()).hexdigest(), "{}", NOW),
        )
        db.execute(
            """INSERT INTO graph_entities
            (entity_id, entity_type, name, properties_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            ("entity-1", "Scenario", "small scene", "{}", NOW),
        )
        db.execute(
            """INSERT INTO graph_entities
            (entity_id, entity_type, name, properties_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            ("entity-2", "Problem", "low score", "{}", NOW),
        )
        db.execute(
            """INSERT INTO graph_relations
            (relation_id, source_entity_id, target_entity_id, relation_type, status, properties_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("relation-1", "entity-1", "entity-2", "HAS_PROBLEM", "observed", "{}", NOW),
        )
        _insert_memory(db)
        db.execute(
            """INSERT INTO knowledge_evidence
            (evidence_id, subject_type, subject_id, source_type, source_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            ("evidence-1", "memory", "memory-1", "document", "document-1", NOW),
        )
        db.execute(
            """INSERT INTO memory_versions
            (memory_id, version, content_json, created_at) VALUES (?, ?, ?, ?)""",
            ("memory-1", 1, '{"status":"candidate"}', NOW),
        )
        db.execute(
            """INSERT INTO summary_runs
            (scene_run_id, summary_method, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)""",
            ("run-1", "reflection_derived", "completed", NOW, NOW),
        )
        db.execute("UPDATE scene_runs SET status = 'completed' WHERE scene_run_id = 'run-1'")
        db.execute("UPDATE memories SET status = 'validated' WHERE memory_id = 'memory-1'")
        db.execute("UPDATE summary_runs SET status = 'skipped' WHERE scene_run_id = 'run-1'")

    with closing(connect_database(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM interaction_events WHERE scene_run_id = 'run-1'").fetchone()[0] == 1
        assert db.execute("SELECT scene_run_id FROM interaction_events WHERE event_id = 'event-outside'").fetchone()[0] is None
        assert db.execute("SELECT content FROM raw_documents WHERE document_id = 'document-1'").fetchone()[0] == content
        assert db.execute("SELECT source_entity_id FROM graph_relations WHERE relation_id = 'relation-1'").fetchone()[0] == "entity-1"
        assert db.execute("SELECT source_id FROM knowledge_evidence WHERE evidence_id = 'evidence-1'").fetchone()[0] == "document-1"
        assert db.execute("SELECT title FROM memories WHERE memory_id = 'memory-1'").fetchone()[0] == "Observed strategy effect"
        assert db.execute("SELECT status FROM memories WHERE memory_id = 'memory-1'").fetchone()[0] == "validated"
        assert json.loads(db.execute("SELECT content_json FROM memory_versions").fetchone()[0]) == {"status": "candidate"}
        assert db.execute("SELECT status FROM summary_runs").fetchone()[0] == "skipped"


def test_constraints_and_transaction_rollback(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    initialize_database(path)
    with transaction(path) as db:
        _insert_run(db)
        _insert_memory(db)

    invalid_statements = (
        ("INSERT INTO interaction_events (event_id, session_id, sequence_no, event_type, payload_json) VALUES ('e', 's', 1, 'x', '{}')", None),
        ("INSERT INTO interaction_events (event_id, session_id, sequence_no, event_type, payload_json) VALUES ('e2', 's', 1, 'x', '{}')", "unique"),
        ("INSERT INTO graph_relations (relation_id, source_entity_id, target_entity_id, relation_type, status, properties_json, created_at) VALUES ('bad', 'missing', 'missing', 'HAS_PROBLEM', 'observed', '{}', 'now')", "foreign key"),
        ("INSERT INTO memory_versions (memory_id, version, content_json, created_at) VALUES ('missing', 1, '{}', 'now')", "foreign key"),
        ("INSERT INTO raw_documents (document_id, document_type, content, content_hash, metadata_json, created_at) VALUES ('bad', 'scene', 'x', 'hash', 'invalid', 'now')", "check"),
        ("UPDATE memories SET confidence = 1.5 WHERE memory_id = 'memory-1'", "check"),
        ("UPDATE memories SET status = 'invented' WHERE memory_id = 'memory-1'", "check"),
    )
    with transaction(path) as db:
        db.execute(invalid_statements[0][0])
    for sql, reason in invalid_statements[1:]:
        with pytest.raises(sqlite3.IntegrityError, match=f"(?i){reason}"):
            with transaction(path) as db:
                db.execute(sql)

    with pytest.raises(sqlite3.IntegrityError, match="(?i)unique"):
        with transaction(path) as db:
            db.execute(
                """INSERT INTO interaction_events
                (event_id, session_id, sequence_no, event_type, payload_json)
                VALUES ('e', 'different-session', 2, 'x', '{}')"""
            )

    with pytest.raises(sqlite3.IntegrityError):
        with transaction(path) as db:
            db.execute("INSERT INTO graph_entities (entity_id, entity_type, name, properties_json, created_at) VALUES ('rollback', 'Scenario', 'x', '{}', 'now')")
            db.execute("UPDATE memories SET confidence = 1.5 WHERE memory_id = 'memory-1'")
    with closing(connect_database(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM graph_entities WHERE entity_id = 'rollback'").fetchone()[0] == 0


def test_memory_versions_remain_independent_after_reopen(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    initialize_database(path)
    with transaction(path) as db:
        _insert_run(db)
        _insert_memory(db)
        for version in (1, 2):
            db.execute(
                "INSERT INTO memory_versions (memory_id, version, content_json, created_at) VALUES (?, ?, ?, ?)",
                ("memory-1", version, json.dumps({"version": version}), NOW),
            )
    with closing(connect_database(path)) as db:
        assert [json.loads(row[0])["version"] for row in db.execute(
            "SELECT content_json FROM memory_versions WHERE memory_id = ? ORDER BY version", ("memory-1",)
        )] == [1, 2]


def test_failed_migration_rolls_back_tables_version_and_preserves_reflections(tmp_path, monkeypatch):
    from rade_memory import migrations

    path = tmp_path / "knowledge.sqlite3"
    service = KnowledgeService(path)
    proposal, source, scene, records = comparison((10, 10), "before-failure")
    reflection = service.propose(proposal, source, scene, records.__getitem__)
    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_read_schema", lambda: "CREATE TABLE migration_probe (id INTEGER);\nCREATE TABLE broken (;\n")
        with pytest.raises(sqlite3.OperationalError):
            initialize_database(path)

    assert current_schema_version(path) is None
    with closing(connect_database(path)) as db:
        assert "migration_probe" not in _tables(db)
        assert "schema_version" not in _tables(db)
        assert {"reflections", "knowledge_revisions"} <= _tables(db)
    assert service.explain(reflection["reflection_id"]) == reflection

    initialize_database(path)
    assert current_schema_version(path) == 1
