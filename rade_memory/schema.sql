CREATE TABLE scene_runs (
    scene_run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    scene_digest TEXT,
    scene_filename TEXT,
    mode TEXT CHECK (mode IN ('rethink', 'norethink')),
    status TEXT CHECK (status IN ('running', 'completed', 'failed', 'timed_out', 'cancelled', 'interrupted')),
    baseline_experiment_id TEXT,
    selected_experiment_id TEXT,
    baseline_expected_value REAL,
    final_expected_value REAL,
    started_at TEXT,
    finished_at TEXT,
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json))
);
CREATE INDEX scene_runs_session ON scene_runs (session_id);
CREATE INDEX scene_runs_scene ON scene_runs (scene_digest);

CREATE TABLE interaction_events (
    event_id TEXT PRIMARY KEY,
    scene_run_id TEXT REFERENCES scene_runs (scene_run_id),
    session_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    event_type TEXT NOT NULL,
    turn_id TEXT,
    tool_call_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    timestamp_utc TEXT,
    UNIQUE (session_id, sequence_no)
);
CREATE INDEX interaction_events_run ON interaction_events (scene_run_id);
CREATE INDEX interaction_events_session ON interaction_events (session_id);

CREATE TABLE raw_documents (
    document_id TEXT PRIMARY KEY,
    scene_run_id TEXT REFERENCES scene_runs (scene_run_id),
    scene_digest TEXT,
    document_type TEXT NOT NULL CHECK (document_type IN (
        'scene', 'reflection', 'experiment', 'final_result', 'model_output', 'error'
    )),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_path TEXT,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL
);
CREATE INDEX raw_documents_run ON raw_documents (scene_run_id);
CREATE INDEX raw_documents_hash ON raw_documents (content_hash);

CREATE TABLE graph_entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN (
        'Scenario', 'Problem', 'Cause', 'Strategy', 'Experiment', 'Outcome', 'Experience'
    )),
    name TEXT NOT NULL,
    description TEXT,
    canonical_key TEXT,
    properties_json TEXT NOT NULL CHECK (json_valid(properties_json)),
    created_at TEXT NOT NULL
);
CREATE INDEX graph_entities_type_key ON graph_entities (entity_type, canonical_key);

CREATE TABLE graph_relations (
    relation_id TEXT PRIMARY KEY,
    source_entity_id TEXT NOT NULL REFERENCES graph_entities (entity_id),
    target_entity_id TEXT NOT NULL REFERENCES graph_entities (entity_id),
    relation_type TEXT NOT NULL,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('hypothesis', 'observed', 'validated', 'rejected')),
    properties_json TEXT NOT NULL CHECK (json_valid(properties_json)),
    created_at TEXT NOT NULL
);
CREATE INDEX graph_relations_source ON graph_relations (source_entity_id);
CREATE INDEX graph_relations_target ON graph_relations (target_entity_id);

CREATE TABLE knowledge_evidence (
    evidence_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX knowledge_evidence_subject ON knowledge_evidence (subject_type, subject_id);
CREATE INDEX knowledge_evidence_source ON knowledge_evidence (source_type, source_id);

CREATE TABLE memories (
    memory_id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    source_scene_run_id TEXT REFERENCES scene_runs (scene_run_id),
    source_reflection_id TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    problem TEXT,
    cause TEXT,
    strategy TEXT,
    outcome TEXT,
    experience_rule TEXT,
    scenario_features_json TEXT NOT NULL CHECK (json_valid(scenario_features_json)),
    applicable_conditions_json TEXT NOT NULL CHECK (json_valid(applicable_conditions_json)),
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN (
        'candidate', 'validated', 'conflicted', 'rejected', 'needs_review'
    )),
    summary_method TEXT NOT NULL CHECK (summary_method IN (
        'reflection_derived', 'llm_extended', 'deterministic'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX memories_source_run ON memories (source_scene_run_id);
CREATE INDEX memories_status_created ON memories (status, created_at);

CREATE TABLE memory_versions (
    memory_id TEXT NOT NULL REFERENCES memories (memory_id),
    version INTEGER NOT NULL CHECK (version > 0),
    content_json TEXT NOT NULL CHECK (json_valid(content_json)),
    change_reason TEXT,
    evidence_json TEXT CHECK (evidence_json IS NULL OR json_valid(evidence_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, version)
);

CREATE TABLE summary_runs (
    scene_run_id TEXT NOT NULL REFERENCES scene_runs (scene_run_id),
    summary_method TEXT NOT NULL CHECK (summary_method IN (
        'reflection_derived', 'llm_extended', 'deterministic'
    )),
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'skipped')),
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scene_run_id, summary_method)
);
CREATE INDEX summary_runs_status ON summary_runs (status);

CREATE TABLE schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL CHECK (version > 0)
);
