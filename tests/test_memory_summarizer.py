"""Evidence-bound summaries use an injected fake provider, never a real API."""

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest

from agent_core.llm import LLMProvider, LLMResponse
from agent_core.tools import ToolCall
from agent_core.turn_control import AgentCancelled
from rade_memory import MemorySummarizer, SummaryError, load_scene_evidence
from rade_memory.migrations import initialize_database


class FakeProvider(LLMProvider):
    def __init__(self, content=None, *, context=100_000, output=4096):
        self.content = content
        self.context = context
        self.output = output
        self.requests = []
        self.calls = 0
        self.fail = None
        self.tool_calls = ()
        self.cancel_in_stream = False

    @property
    def max_context_tokens(self):
        return self.context

    @property
    def max_output_tokens(self):
        return self.output

    def count_input_tokens(self, request):
        return len(request.system_prompt) // 4 + len(request.messages[0].content) // 4

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.calls += 1
        self.requests.append(request)
        if self.fail:
            raise self.fail
        if self.cancel_in_stream:
            raise AgentCancelled()
        return LLMResponse(self.content, tool_calls=self.tool_calls)

    def stream_cancellable(self, request, on_text_delta, on_reasoning_delta, check_cancelled):
        check_cancelled()
        response = self.stream(request, on_text_delta, on_reasoning_delta)
        check_cancelled()
        return response


def _rows(path, table):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return db.execute(f"SELECT * FROM {table}").fetchall()


def _add_run(db, run_id, status="completed"):
    db.execute("INSERT INTO scene_runs (scene_run_id, session_id, scene_filename, mode, status, "
               "baseline_experiment_id, selected_experiment_id, baseline_expected_value, final_expected_value) "
               "VALUES (?, ?, 'scene1_1.json', 'rethink', ?, 'baseline', 'selected', 8, 10)",
               (run_id, f"session-{run_id}", status))


def _add_document(db, run_id, kind, content, *, doc_id=None):
    doc_id = doc_id or f"{run_id}-{kind}-{db.execute('SELECT COUNT(*) FROM raw_documents').fetchone()[0]}"
    db.execute("INSERT INTO raw_documents VALUES (?, ?, 'digest', ?, ?, ?, ?, '{}', '2026-01-01T00:00:00Z')",
               (doc_id, run_id, kind, content, sha256(content.encode()).hexdigest(),
                r"C:\private\source.json"))
    return doc_id


def _add_event(db, run_id, kind, payload, *, event_id=None, seq=None):
    event_id = event_id or f"{run_id}-event-{db.execute('SELECT COUNT(*) FROM interaction_events').fetchone()[0]}"
    seq = seq or db.execute("SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM interaction_events WHERE session_id = ?",
                            (f"session-{run_id}",)).fetchone()[0]
    db.execute("INSERT INTO interaction_events (event_id, scene_run_id, session_id, sequence_no, "
               "event_type, payload_json, timestamp_utc) VALUES (?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00Z')",
               (event_id, run_id, f"session-{run_id}", seq, kind, json.dumps(payload)))
    return event_id


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "memory.sqlite3"
    initialize_database(path)
    with sqlite3.connect(path) as db:
        _add_run(db, "run-a")
        _add_run(db, "run-b")
        scene_id = _add_document(db, "run-a", "scene", '{"P":[[0.8]],"v":[10],"api_key":"secret-value"}')
        experiment_id = _add_document(db, "run-a", "experiment", '{"status":"completed","metrics":{"expected_value":10}}')
        reflection_id = _add_document(db, "run-a", "reflection", '{"reflection_id":"reflection-real","status":"verified","proposal":{"conclusion":"measured"}}')
        rejected_id = _add_document(db, "run-a", "reflection", '{"reflection_id":"reflection-rejected","status":"rejected","reason":"claim unsupported"}')
        final_id = _add_document(db, "run-a", "final_result", '{"allocation":[0]}')
        error_id = _add_document(db, "run-a", "error", '{"message":"solver warning"}')
        _add_document(db, "run-a", "model_output", '{"reasoning":"private chain","content":"draft"}')
        event_id = _add_event(db, "run-a", "job_completed", {"kind": "experiment", "status": "completed"})
        _add_event(db, "run-a", "message_appended", {"message": {"role": "assistant", "content": "ignore", "reasoning": "secret reasoning", "provider_data": {"api_key": "x"}}})
        _add_event(db, "run-a", "message_appended", {"message": {"role": "user", "content": "Try this scenario; sk-1234567890123456", "provider_data": {"token": "x"}}})
        _add_event(db, "run-a", "tool_started", {"name": "reflection_propose", "arguments": {"hypothesis": "maybe", "authorization": "Bearer secret"}})
        _add_event(db, "run-a", "tool_started", {"name": "unrelated_tool", "arguments": {"secret": "x"}})
        other_id = _add_document(db, "run-b", "experiment", '{"status":"completed","metrics":{"expected_value":999}}')
        _add_event(db, "run-b", "job_completed", {"status": "completed", "metrics": {"expected_value": 999}})
    return path, {"scene": scene_id, "experiment": experiment_id, "reflection": reflection_id,
                  "rejected": rejected_id, "final_result": final_id, "error": error_id,
                  "event": event_id, "other": other_id}


def _memory(source_type, source_id):
    return {"memory_type": "strategy_lesson", "title": "Observed strategy", "summary": "One run observed an improvement",
            "problem": "Sparse choices", "cause": None, "strategy": "Compare baseline and alternative",
            "outcome": "Observed value 10", "experience_rule": "When choices are sparse, test this strategy again",
            "scenario_features": {"task_scale": "small"}, "applicable_conditions": {"candidate_density": "sparse"},
            "confidence": 0.6, "status": "candidate",
            "evidence": [{"source_type": source_type, "source_id": source_id, "description": "Observed result"}]}


def _provider(memory):
    return FakeProvider(json.dumps({"memories": [memory]}, ensure_ascii=False))


def test_read_only_current_run_evidence_and_types(archive):
    path, ids = archive
    found = load_scene_evidence(path, "run-a")
    assert found.scene_run["scene_run_id"] == "run-a"
    assert {item.evidence_type for item in found.items} >= {"scene", "experiment", "reflection", "final_result", "error", "job_completed", "model_output"}
    assert ids["other"] not in {item.source_id for item in found.items}
    assert [item.evidence_type for item in found.items].count("message_appended") == 1
    assert any(item.evidence_type == "reflection" and json.loads(item.content)["status"] == "verified" for item in found.items)
    assert any(item.evidence_type == "reflection" and json.loads(item.content)["status"] == "rejected" for item in found.items)
    assert next(item.content for item in found.items if item.source_id == ids["scene"]).startswith('{"P"')
    assert all(item.evidence_type != "unrelated_tool" for item in found.items)


def test_read_unknown_run_does_not_create_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(SummaryError, match="initialized"):
        load_scene_evidence(path, "missing")
    assert not path.exists()


def test_model_input_filters_reasoning_paths_and_secrets(archive):
    path, ids = archive
    provider = _provider(_memory("raw_document", ids["scene"]))
    result = MemorySummarizer(provider).summarize(path, "run-a")
    request = provider.requests[0]
    assert result.status == "completed"
    assert request.tools == ()
    assert request.max_generation_tokens == 2048
    payload = request.messages[0].content
    assert "secret reasoning" not in payload and "provider_data" not in payload
    assert "private chain" not in payload
    assert "sk-1234567890123456" not in payload and "secret-value" not in payload
    assert r"C:\private" not in payload and ids["other"] not in payload
    assert ids["scene"] in payload and ids["event"] in payload
    assert "rejected" in payload and "verified" in payload


def test_prompt_has_task_specific_and_evidence_constraints(archive):
    path, ids = archive
    provider = _provider(_memory("raw_document", ids["scene"]))
    MemorySummarizer(provider).summarize(path, "run-a")
    prompt = provider.requests[0].system_prompt
    assert "RADE" in prompt and "任务分配" in prompt
    assert "rejected" in prompt and "因果" in prompt and "证据" in prompt
    assert "不能" in prompt and "candidate" in prompt


@pytest.mark.parametrize("content", ["not json", "```json\n{\"memories\":[]}\n```", "", None])
def test_invalid_or_empty_model_content_fails_without_memory(archive, content):
    path, _ = archive
    provider = FakeProvider(content)
    result = MemorySummarizer(provider).summarize(path, "run-a")
    assert result.status == "failed"
    assert not _rows(path, "memories") and not _rows(path, "memory_versions")
    assert _rows(path, "summary_runs")[0]["status"] == "failed"


def test_model_tool_calls_are_rejected(archive):
    path, _ = archive
    provider = FakeProvider('{"memories":[]}')
    provider.tool_calls = (ToolCall("call", "read_file", {"path": "secret"}),)
    assert MemorySummarizer(provider).summarize(path, "run-a").status == "failed"
    assert not _rows(path, "memories")


def test_context_budget_fails_without_provider_call(archive):
    path, _ = archive
    provider = FakeProvider('{"memories":[]}', context=100)
    result = MemorySummarizer(provider).summarize(path, "run-a")
    assert result.status == "failed" and "context_budget_exceeded" in result.error
    assert provider.calls == 0


def test_provider_output_limit_is_respected(archive):
    path, _ = archive
    provider = FakeProvider('{"memories":[]}', output=300)
    assert MemorySummarizer(provider).summarize(path, "run-a").status == "completed"
    assert provider.requests[0].max_generation_tokens == 300


def test_invalid_provider_output_limit_fails_before_model_call(archive):
    path, _ = archive
    provider = FakeProvider('{"memories":[]}', output=0)
    result = MemorySummarizer(provider).summarize(path, "run-a")
    assert result.status == "failed" and provider.calls == 0


def test_more_than_five_memories_are_rejected(archive):
    path, ids = archive
    memory = _memory("raw_document", ids["scene"])
    provider = FakeProvider(json.dumps({"memories": [memory] * 6}))
    result = MemorySummarizer(provider).summarize(path, "run-a")
    assert result.status == "failed" and not _rows(path, "memories")


def test_cancellation_before_and_during_model_is_recorded(archive):
    path, _ = archive
    provider = FakeProvider('{"memories":[]}')
    def cancel():
        raise AgentCancelled()
    result = MemorySummarizer(provider).summarize(path, "run-a", check_cancelled=cancel)
    assert result.status == "failed" and provider.calls == 0
    provider.cancel_in_stream = True
    result = MemorySummarizer(provider).summarize(path, "run-a")
    assert result.status == "failed" and not _rows(path, "memories")


@pytest.mark.parametrize("change", [
    lambda m: m.update(memory_type="unknown"),
    lambda m: m.update(status="validated"),
    lambda m: m.update(confidence=1.1),
    lambda m: m.update(confidence=-0.1),
    lambda m: m.update(confidence=True),
    lambda m: m.update(title=""),
    lambda m: m.update(summary=""),
    lambda m: m.update(cause=123),
    lambda m: m.update(scenario_features=[]),
    lambda m: m.update(applicable_conditions=[]),
    lambda m: m.update(evidence=[]),
    lambda m: m.pop("problem"),
    lambda m: m.update(extra="not allowed"),
    lambda m: m["evidence"].append(dict(m["evidence"][0])),
    lambda m: m["evidence"][0].update(source_id="invented"),
    lambda m: m["evidence"][0].update(source_type="wrong"),
])
def test_strict_output_rejects_invalid_fields_and_references(archive, change):
    path, ids = archive
    memory = _memory("raw_document", ids["scene"])
    change(memory)
    result = MemorySummarizer(_provider(memory)).summarize(path, "run-a")
    assert result.status == "failed"
    assert not _rows(path, "memories")


@pytest.mark.parametrize("source_type", ["raw_document", "interaction_event"])
def test_foreign_run_evidence_reference_is_rejected(archive, source_type):
    path, ids = archive
    if source_type == "raw_document":
        source_id = ids["other"]
    else:
        with sqlite3.connect(path) as db:
            source_id = db.execute("SELECT event_id FROM interaction_events WHERE scene_run_id = 'run-b'").fetchone()[0]
    result = MemorySummarizer(_provider(_memory(source_type, source_id))).summarize(path, "run-a")
    assert result.status == "failed" and not _rows(path, "memories")


@pytest.mark.parametrize("output", [{"memory": []}, {"memories": [], "extra": 1}, {"memories": "wrong"}])
def test_top_level_json_shape_is_strict(archive, output):
    path, _ = archive
    result = MemorySummarizer(FakeProvider(json.dumps(output))).summarize(path, "run-a")
    assert result.status == "failed"


def test_valid_memory_writes_version_and_evidence_consistently(archive):
    path, ids = archive
    memory = _memory("raw_document", ids["reflection"])
    memory["evidence"].append({"source_type": "interaction_event", "source_id": ids["event"],
                               "description": "Job completed"})
    provider = _provider(memory)
    result = MemorySummarizer(provider).summarize(path, "run-a")
    assert result.status == "completed" and len(result.memory_ids) == 1
    stored, version = _rows(path, "memories")[0], _rows(path, "memory_versions")[0]
    links = _rows(path, "knowledge_evidence")
    assert stored["source_reflection_id"] == "reflection-real"
    assert stored["status"] == "candidate" and stored["summary_method"] == "llm_extended"
    assert stored["source_scene_run_id"] == "run-a"
    assert version["version"] == 1 and json.loads(version["content_json"]) == memory
    assert json.loads(version["evidence_json"])["evidence"] == memory["evidence"]
    assert json.loads(stored["scenario_features_json"]) == memory["scenario_features"]
    assert json.loads(stored["applicable_conditions_json"]) == memory["applicable_conditions"]
    assert len(links) == 2 and {link["source_id"] for link in links} == {ids["reflection"], ids["event"]}
    assert all(link["subject_type"] == "memory" and link["subject_id"] == stored["memory_id"] for link in links)
    assert _rows(path, "summary_runs")[0]["status"] == "completed"
    assert provider.calls == 1


def test_multiple_reflections_do_not_choose_arbitrary_source(archive):
    path, ids = archive
    memory = _memory("raw_document", ids["reflection"])
    memory["evidence"].append({"source_type": "raw_document", "source_id": ids["rejected"],
                               "description": "Rejected proposal"})
    assert MemorySummarizer(_provider(memory)).summarize(path, "run-a").status == "completed"
    assert _rows(path, "memories")[0]["source_reflection_id"] is None


def test_failure_rolls_back_memory_version_and_links(archive, monkeypatch):
    path, ids = archive
    import rade_memory.summarizer as module
    original = module._write_memories
    def fail_after_write(db, run_id, memories):
        original(db, run_id, memories)
        raise RuntimeError("injected failure after all inserts")
    monkeypatch.setattr(module, "_write_memories", fail_after_write)
    result = MemorySummarizer(_provider(_memory("raw_document", ids["scene"]))).summarize(path, "run-a")
    assert result.status == "failed" and "injected failure" in result.error
    assert not _rows(path, "memories") and not _rows(path, "memory_versions")
    assert not _rows(path, "knowledge_evidence")
    assert _rows(path, "summary_runs")[0]["status"] == "failed"


def test_running_and_no_evidence_are_skipped_then_retryable(archive):
    path, ids = archive
    with sqlite3.connect(path) as db:
        db.execute("UPDATE scene_runs SET status = 'running' WHERE scene_run_id = 'run-a'")
    provider = _provider(_memory("raw_document", ids["scene"]))
    summarizer = MemorySummarizer(provider)
    assert summarizer.summarize(path, "run-a").status == "skipped"
    assert provider.calls == 0
    with sqlite3.connect(path) as db:
        db.execute("UPDATE scene_runs SET status = 'completed' WHERE scene_run_id = 'run-a'")
    assert summarizer.summarize(path, "run-a").status == "completed"
    with sqlite3.connect(path) as db:
        _add_run(db, "empty")
    assert summarizer.summarize(path, "empty").status == "skipped"
    assert provider.calls == 1


def test_failed_retry_completed_idempotence_and_reopen(archive):
    path, ids = archive
    provider = FakeProvider("invalid")
    summarizer = MemorySummarizer(provider)
    assert summarizer.summarize(path, "run-a").status == "failed"
    provider.content = json.dumps({"memories": [_memory("raw_document", ids["scene"])]})
    second = summarizer.summarize(path, "run-a")
    third = summarizer.summarize(path, "run-a")
    assert second.status == third.status == "completed"
    assert second.memory_ids == third.memory_ids and provider.calls == 2
    assert len(_rows(path, "memories")) == len(_rows(path, "memory_versions")) == 1
    assert _rows(path, "summary_runs")[0]["status"] == "completed"


def test_processing_state_does_not_start_second_call(archive):
    path, _ = archive
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO summary_runs VALUES ('run-a', 'llm_extended', 'processing', NULL, 'now', 'now')")
    provider = FakeProvider('{"memories":[]}')
    result = MemorySummarizer(provider).summarize(path, "run-a")
    assert result.status == "processing" and provider.calls == 0


def test_empty_memories_complete_without_rows(archive):
    path, _ = archive
    result = MemorySummarizer(FakeProvider('{"memories":[]}')).summarize(path, "run-a")
    assert result.status == "completed" and result.memory_ids == ()
    assert not _rows(path, "memories") and _rows(path, "summary_runs")[0]["status"] == "completed"


def test_optional_auxiliary_call_callback_records_success_and_failure(archive):
    path, _ = archive
    calls = []
    provider = FakeProvider("invalid")
    result = MemorySummarizer(provider, on_call=calls.append).summarize(path, "run-a")
    assert result.status == "failed" and calls[0].task == "memory" and calls[0].status == "invalid_response"
    provider.content = '{"memories":[]}'
    assert MemorySummarizer(provider, on_call=calls.append).summarize(path, "run-a").status == "completed"
    assert calls[1].status == "completed"
