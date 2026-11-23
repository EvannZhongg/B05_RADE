"""Explicit, evidence-bound LLM summaries for one archived RADE scene run."""

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Callable
from uuid import uuid4

from agent_core.auxiliary import AuxiliaryCallTracker
from agent_core.llm import AuxiliaryModelCall, LLMProvider, LLMRequest
from agent_core.session import Message
from agent_core.turn_control import AgentCancelled

from .database import transaction
from .migrations import SCHEMA_VERSION, current_schema_version


METHOD = "llm_extended"
TERMINAL = {"completed", "failed", "timed_out", "cancelled", "interrupted"}
TYPES = {"strategy_lesson", "failure_lesson", "scenario_pattern", "constraint_lesson",
         "evaluation_lesson", "decision_lesson"}
STATUSES = {"candidate", "needs_review"}
SOURCE_TYPES = {"raw_document", "interaction_event"}
FIELDS = {"memory_type", "title", "summary", "problem", "cause", "strategy", "outcome",
          "experience_rule", "scenario_features", "applicable_conditions", "confidence",
          "status", "evidence"}
EVIDENCE_FIELDS = {"source_type", "source_id", "description"}
EVENT_TYPES = {"application_scene_task", "job_submitted", "job_started", "job_completed",
               "job_failed", "job_cancelled", "job_interrupted", "tool_started",
               "tool_completed", "tool_failed", "tool_cancelled"}
TOOLS = {"strategy_list", "experiment_run", "experiment_get", "knowledge_query",
         "knowledge_explain", "reflection_propose"}
SECRET_KEYS = {"api_key", "apikey", "authorization", "access_token", "refresh_token",
               "password", "secret", "token"}
DROP_KEYS = {"provider_data", "reasoning", "source_path", "output_path"}
SECRET_PATTERN = re.compile(r"(?i)(bearer\s+)[^\s\"']+|(sk-[A-Za-z0-9_-]{8,})|"
                            r"((?:api[_-]?key|password|secret|token)[\"']?\s*[:=]\s*[\"']?)[^\s,\"']+")
PATH_PATTERN = re.compile(r"(?i)(?:[A-Z]:\\[^\s\"']+|(?<![:/])/(?:[^\s/\"']+/)+[^\s\"']+)")


class SummaryError(ValueError):
    """The archived evidence, model response, or context budget is unsuitable."""


@dataclass(frozen=True)
class Evidence:
    source_type: str
    source_id: str
    evidence_type: str
    content: object
    source_path: str | None = None
    content_hash: str | None = None
    metadata_json: str | None = None


@dataclass(frozen=True)
class SceneEvidence:
    scene_run: dict[str, object]
    items: tuple[Evidence, ...]


@dataclass(frozen=True)
class SummaryResult:
    status: str
    scene_run_id: str
    memory_ids: tuple[str, ...] = ()
    error: str | None = None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe(value: object) -> object:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in SECRET_KEYS else _safe(item)
                for key, item in value.items() if key.lower() not in DROP_KEYS}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        value = SECRET_PATTERN.sub(lambda m: (m.group(1) or m.group(3) or "") + "[REDACTED]", value)
        return PATH_PATTERN.sub("[LOCAL_PATH]", value)
    return value


def _document_content(content: str) -> object:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = content
    return _safe(parsed)


def load_scene_evidence(database: Path, scene_run_id: str) -> SceneEvidence:
    """Read only this run's archived evidence without creating a database."""
    if current_schema_version(database) != SCHEMA_VERSION:
        raise SummaryError("domain-memory database must be initialized")
    db = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT * FROM scene_runs WHERE scene_run_id = ?", (scene_run_id,)).fetchone()
        if row is None:
            raise SummaryError(f"unknown scene_run_id: {scene_run_id}")
        run = dict(row)
        items = []
        for document in db.execute("SELECT * FROM raw_documents WHERE scene_run_id = ? ORDER BY created_at, document_id",
                                   (scene_run_id,)):
            if document["document_type"] == "model_output":
                # Only structured output can have reasoning fields removed reliably.
                try:
                    if not isinstance(json.loads(document["content"]), dict):
                        continue
                except json.JSONDecodeError:
                    continue
            items.append(Evidence("raw_document", document["document_id"], document["document_type"],
                                  document["content"], document["source_path"],
                                  document["content_hash"], document["metadata_json"]))
        for event in db.execute("SELECT * FROM interaction_events WHERE scene_run_id = ? ORDER BY sequence_no",
                                (scene_run_id,)):
            kind = event["event_type"]
            payload = json.loads(event["payload_json"])
            if kind == "message_appended":
                message = payload.get("message")
                if not isinstance(message, dict) or message.get("role") != "user" or not message.get("content"):
                    continue
                content: object = {"role": "user", "content": _safe(message["content"])}
            elif kind in EVENT_TYPES:
                if kind.startswith("tool_") and payload.get("name") not in TOOLS:
                    continue
                content = _safe(payload)
            else:
                continue
            items.append(Evidence("interaction_event", event["event_id"], kind, content))
        return SceneEvidence(run, tuple(items))
    finally:
        db.close()


def _model_run(run: dict[str, object]) -> dict[str, object]:
    return _safe({key: value for key, value in run.items() if key != "error_json"} |
                 ({"error": json.loads(run["error_json"])} if run.get("error_json") else {}))


def _validate_output(content: str, available: set[tuple[str, str]]) -> list[dict[str, object]]:
    try:
        output = json.loads(content)
    except (json.JSONDecodeError, TypeError) as error:
        raise SummaryError("model response must be plain JSON") from error
    if not isinstance(output, dict) or set(output) != {"memories"} or not isinstance(output["memories"], list):
        raise SummaryError("model response must contain only a memories array")
    if len(output["memories"]) > 5:
        raise SummaryError("at most five memories are allowed")
    for memory in output["memories"]:
        if not isinstance(memory, dict) or set(memory) != FIELDS:
            raise SummaryError("memory fields are incomplete or unexpected")
        if memory["memory_type"] not in TYPES or memory["status"] not in STATUSES:
            raise SummaryError("invalid memory_type or status")
        for field in ("title", "summary", "experience_rule"):
            if not isinstance(memory[field], str) or not memory[field].strip():
                raise SummaryError(f"{field} must be nonempty text")
        for field in ("problem", "cause", "strategy", "outcome"):
            if memory[field] is not None and not isinstance(memory[field], str):
                raise SummaryError(f"{field} must be text or null")
        for field in ("scenario_features", "applicable_conditions"):
            if not isinstance(memory[field], dict):
                raise SummaryError(f"{field} must be a JSON object")
        confidence = memory["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise SummaryError("confidence must be a finite number from 0 to 1")
        evidence = memory["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise SummaryError("each memory needs evidence")
        seen: set[tuple[str, str]] = set()
        for reference in evidence:
            if not isinstance(reference, dict) or set(reference) != EVIDENCE_FIELDS:
                raise SummaryError("invalid evidence reference fields")
            if reference["source_type"] not in SOURCE_TYPES or not isinstance(reference["source_id"], str):
                raise SummaryError("invalid evidence source")
            key = (reference["source_type"], reference["source_id"])
            if key not in available:
                raise SummaryError("evidence ID does not belong to this scene run")
            if key in seen:
                raise SummaryError("duplicate evidence reference")
            if reference["description"] is not None and not isinstance(reference["description"], str):
                raise SummaryError("evidence description must be text or null")
            seen.add(key)
    return output["memories"]


def _memory_ids(db: sqlite3.Connection, scene_run_id: str) -> tuple[str, ...]:
    rows = db.execute("SELECT memory_id FROM memories WHERE source_scene_run_id = ? AND summary_method = ? ORDER BY created_at, memory_id",
                      (scene_run_id, METHOD))
    return tuple(row[0] for row in rows)


def _set_status(db: sqlite3.Connection, scene_run_id: str, status: str, error: str | None) -> None:
    db.execute("UPDATE summary_runs SET status = ?, error_message = ?, updated_at = ? "
               "WHERE scene_run_id = ? AND summary_method = ?", (status, error, _now(), scene_run_id, METHOD))


def _source_reflection_id(db: sqlite3.Connection, references: list[dict[str, object]]) -> str | None:
    ids = set()
    for reference in references:
        if reference["source_type"] != "raw_document":
            continue
        row = db.execute("SELECT document_type, content FROM raw_documents WHERE document_id = ?",
                         (reference["source_id"],)).fetchone()
        if row is not None and row["document_type"] == "reflection":
            content = json.loads(row["content"])
            reflection_id = content.get("reflection_id")
            if isinstance(reflection_id, str):
                ids.add(reflection_id)
    return next(iter(ids)) if len(ids) == 1 else None


def _write_memories(db: sqlite3.Connection, run_id: str, memories: list[dict[str, object]]) -> tuple[str, ...]:
    ids = []
    for memory in memories:
        for ref in memory["evidence"]:
            table, column = ("raw_documents", "document_id") if ref["source_type"] == "raw_document" else ("interaction_events", "event_id")
            found = db.execute(f"SELECT 1 FROM {table} WHERE {column} = ? AND scene_run_id = ?",
                               (ref["source_id"], run_id)).fetchone()
            if found is None:
                raise SummaryError("evidence disappeared or changed scene ownership")
        memory_id, now = uuid4().hex, _now()
        ids.append(memory_id)
        db.execute("""INSERT INTO memories (
            memory_id, memory_type, source_scene_run_id, source_reflection_id, title, summary,
            problem, cause, strategy, outcome, experience_rule, scenario_features_json,
            applicable_conditions_json, confidence, status, summary_method, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (memory_id, memory["memory_type"], run_id, _source_reflection_id(db, memory["evidence"]),
                    memory["title"], memory["summary"], memory["problem"], memory["cause"],
                    memory["strategy"], memory["outcome"], memory["experience_rule"],
                    _json(memory["scenario_features"]), _json(memory["applicable_conditions"]),
                    memory["confidence"], memory["status"], METHOD, now, now))
        db.execute("""INSERT INTO memory_versions
            (memory_id, version, content_json, change_reason, evidence_json, created_at)
            VALUES (?, 1, ?, ?, ?, ?)""",
                   (memory_id, _json(memory), "Initial LLM summary", _json({"evidence": memory["evidence"]}), now))
        for ref in memory["evidence"]:
            db.execute("""INSERT INTO knowledge_evidence
                (evidence_id, subject_type, subject_id, source_type, source_id, description, created_at)
                VALUES (?, 'memory', ?, ?, ?, ?, ?)""",
                       (uuid4().hex, memory_id, ref["source_type"], ref["source_id"], ref["description"], now))
    return tuple(ids)


class MemorySummarizer:
    def __init__(self, provider: LLMProvider, *, prompt: str | None = None,
                 max_generation_tokens: int = 2048,
                 on_call: Callable[[AuxiliaryModelCall], None] | None = None) -> None:
        if isinstance(max_generation_tokens, bool) or not isinstance(max_generation_tokens, int) or not 1 <= max_generation_tokens <= 2048:
            raise ValueError("max_generation_tokens must be between 1 and 2048")
        self.provider = provider
        self.prompt = prompt if prompt is not None else files("rade_memory").joinpath("summarizer_prompt.md").read_text(encoding="utf-8")
        self.max_generation_tokens = max_generation_tokens
        self.on_call = on_call

    def summarize(self, database: Path, scene_run_id: str, *,
                  check_cancelled: Callable[[], None] | None = None) -> SummaryResult:
        evidence = load_scene_evidence(database, scene_run_id)
        cancelled = check_cancelled or (lambda: None)
        with transaction(database) as db:
            prior = db.execute("SELECT status FROM summary_runs WHERE scene_run_id = ? AND summary_method = ?",
                               (scene_run_id, METHOD)).fetchone()
            if prior is not None and prior["status"] == "completed":
                return SummaryResult("completed", scene_run_id, _memory_ids(db, scene_run_id))
            if prior is not None and prior["status"] == "processing":
                return SummaryResult("processing", scene_run_id)
            if prior is None:
                now = _now()
                db.execute("INSERT INTO summary_runs VALUES (?, ?, 'pending', NULL, ?, ?)",
                           (scene_run_id, METHOD, now, now))
            reason = ("scene run is still running" if evidence.scene_run["status"] not in TERMINAL else
                      "no usable archived evidence" if not evidence.items else None)
            if reason is not None:
                _set_status(db, scene_run_id, "skipped", reason)
                return SummaryResult("skipped", scene_run_id, error=reason)
            _set_status(db, scene_run_id, "processing", None)
        try:
            cancelled()
            provider_limit = self.provider.max_output_tokens
            limit = min(self.max_generation_tokens, provider_limit) if provider_limit is not None else self.max_generation_tokens
            if limit < 1:
                raise SummaryError("provider output token limit is invalid")
            model_input = {"scene_run": _model_run(evidence.scene_run),
                           "evidence": [{"source_type": item.source_type, "source_id": item.source_id,
                                         "evidence_type": item.evidence_type,
                                         "content": (_document_content(item.content) if item.source_type == "raw_document"
                                                     else _safe(item.content))}
                                        for item in evidence.items]}
            request = LLMRequest(self.prompt, (Message("user", _json(model_input)),),
                                 max_generation_tokens=limit)
            tokens = self.provider.count_input_tokens(request)
            if tokens + limit > self.provider.max_context_tokens:
                raise SummaryError("context_budget_exceeded: archived evidence exceeds model context")
            with AuxiliaryCallTracker(self.on_call, task="memory", scope=scene_run_id) as call:
                response = call.response = self.provider.stream_cancellable(request, lambda _text: None, None, cancelled)
                if response.tool_calls:
                    raise SummaryError("model tool calls are not allowed")
                if not response.content:
                    raise SummaryError("model returned empty content")
                memories = _validate_output(response.content,
                                            {(item.source_type, item.source_id) for item in evidence.items})
            cancelled()
            with transaction(database) as db:
                row = db.execute("SELECT status FROM summary_runs WHERE scene_run_id = ? AND summary_method = ?",
                                 (scene_run_id, METHOD)).fetchone()
                if row is None or row["status"] != "processing":
                    raise SummaryError("summary run state changed during model call")
                ids = _write_memories(db, scene_run_id, memories)
                _set_status(db, scene_run_id, "completed", None)
            return SummaryResult("completed", scene_run_id, ids)
        except (Exception, AgentCancelled, KeyboardInterrupt) as error:
            reason = str(error) or type(error).__name__
            with transaction(database) as db:
                _set_status(db, scene_run_id, "failed", reason)
            return SummaryResult("failed", scene_run_id, error=reason)
