"""Validate and retain scene-scoped empirical knowledge from allocation experiments."""

import json
import math
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path

from .evaluation import evaluate
from .experiments import ExperimentRecord
from .scene import SceneState


EXPECTED_VALUE_EFFECTS = ("improved", "regressed", "unchanged")


@dataclass(frozen=True)
class ReflectionProposal:
    hypothesis: str
    conclusion: str
    reference_experiment_id: str
    experiment_id: str
    expected_value_effect: str

    def __post_init__(self) -> None:
        for field, limit in (
            ("hypothesis", 2000), ("conclusion", 2000),
            ("reference_experiment_id", 128), ("experiment_id", 128),
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
                raise ValueError(f"{field} must contain 1 to {limit} characters")
            object.__setattr__(self, field, value.strip())
        if self.expected_value_effect not in EXPECTED_VALUE_EFFECTS:
            raise ValueError("expected_value_effect must be improved, regressed or unchanged")


@dataclass(frozen=True)
class ReflectionSource:
    session_id: str
    task_id: str
    filename: str
    scene_digest: str
    source_path: str


ExperimentLookup = Callable[[str], tuple[ExperimentRecord, object]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reflections (
    reflection_id TEXT PRIMARY KEY,
    knowledge_id TEXT,
    version INTEGER,
    scene_digest TEXT NOT NULL,
    direction TEXT,
    payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS reflection_evidence
    ON reflections (knowledge_id, scene_digest, direction) WHERE version IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS reflection_version
    ON reflections (knowledge_id, version) WHERE version IS NOT NULL;

CREATE TABLE IF NOT EXISTS knowledge_revisions (
    knowledge_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (knowledge_id, version)
);
CREATE INDEX IF NOT EXISTS knowledge_scope ON knowledge_revisions (scope, status);
"""


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _identifier(kind: str, value: object) -> str:
    return f"{kind}_{sha256(_encode(value).encode('utf-8')).hexdigest()}"


def _scope(scene: SceneState) -> dict[str, str]:
    workers, tasks = len(scene.snapshot.P), len(scene.snapshot.v)
    density = scene.query("P.positive_fraction")
    assert isinstance(density, (int, float))
    return {
        "task_scale": "small" if tasks < 100 else "large",
        "worker_capacity": "below_task_count" if workers < tasks else "at_least_task_count",
        "candidate_density": "sparse" if density < 0.25 else "dense",
    }


def _profile(experiment: ExperimentRecord) -> dict:
    return {
        "strategy_id": experiment.strategy_id, "config_id": experiment.config_id,
        "version": experiment.version, "seed": experiment.seed,
    }


def _compare(alternative: float, reference: float) -> str:
    if math.isclose(alternative, reference, rel_tol=1e-12, abs_tol=1e-12):
        return "unchanged"
    return "improved" if alternative > reference else "regressed"


def _experiment_evidence(
    proposal: ReflectionProposal, source: ReflectionSource, scene: SceneState,
    lookup: ExperimentLookup,
) -> list[dict]:
    evidence = []
    for experiment_id in (proposal.reference_experiment_id, proposal.experiment_id):
        record, allocation = lookup(experiment_id)
        if record.task_id != source.task_id or record.scene_digest != source.scene_digest:
            raise ValueError(f"Experiment {experiment_id} must belong to the current scene task")
        if record.status != "completed" or record.valid is not True or allocation is None:
            raise ValueError(f"Experiment {experiment_id} must have a completed, valid allocation")
        expected_value = float(evaluate(scene.snapshot, allocation)["expected_value"])
        if record.metrics is None or expected_value != record.metrics.get("expected_value"):
            raise ValueError(f"Experiment {experiment_id} has an inconsistent expected value")
        evidence.append({
            "experiment_id": record.experiment_id, "profile": _profile(record),
            "expected_value": expected_value, "result_ref": record.result_ref,
            "allocation_digest": record.allocation_digest,
        })

    return evidence


class KnowledgeService:
    """Keep reflection records and immutable revisions shared by runs in one workspace."""

    def __init__(self, database: Path) -> None:
        self._database = database

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database)
        try:
            connection.executescript(_SCHEMA)
        except BaseException:
            connection.close()
            raise
        return connection

    def propose(
        self, proposal: ReflectionProposal, source: ReflectionSource, scene: SceneState,
        lookup: ExperimentLookup,
    ) -> dict:
        reflection = {"proposal": asdict(proposal), "source": asdict(source)}
        try:
            evidence = _experiment_evidence(proposal, source, scene, lookup)
            reference, alternative = evidence
            observed = _compare(alternative["expected_value"], reference["expected_value"])
            reflection.update(evidence=evidence, observed_effect=observed)
            if reference["profile"] == alternative["profile"]:
                raise ValueError("Compare two different strategy profiles")
            if observed != proposal.expected_value_effect:
                raise ValueError(f"Claimed effect {proposal.expected_value_effect}, measured effect {observed}")
        except (ValueError, OverflowError) as error:
            reflection.update(status="rejected", reason=str(error))
        else:
            reflection.update(status="verified")

        reflection_id = _identifier("reflection", reflection)
        reflection["reflection_id"] = reflection_id
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT payload FROM reflections WHERE reflection_id = ?", (reflection_id,),
            ).fetchone()
            if existing is not None:
                return json.loads(existing[0])

            knowledge_id, version, direction_for_storage = None, None, None
            if reflection["status"] == "verified":
                # Reversing the submitted comparison must not create a second knowledge entry.
                first, second = sorted(evidence, key=lambda item: _encode(item["profile"]))
                reference, alternative = first["profile"], second["profile"]
                direction = _compare(second["expected_value"], first["expected_value"])
                scope = _scope(scene)
                knowledge_id = _identifier("knowledge", (scope, reference, alternative))
                prior = db.execute(
                    """SELECT reflection_id, version FROM reflections
                       WHERE knowledge_id = ? AND scene_digest = ? AND direction = ?
                       AND version IS NOT NULL""",
                    (knowledge_id, source.scene_digest, direction),
                ).fetchone()
                if prior is None:
                    knowledge = self._revise(db, knowledge_id, scope, reference, alternative, direction)
                    version = knowledge["version"]
                    direction_for_storage = direction
                    reflection.update(knowledge_id=knowledge_id, knowledge_version=version)
                else:
                    reflection.update(
                        knowledge_id=knowledge_id, knowledge_version=prior[1], duplicate_of=prior[0],
                    )
            db.execute(
                "INSERT INTO reflections VALUES (?, ?, ?, ?, ?, ?)",
                (reflection_id, knowledge_id, version, source.scene_digest, direction_for_storage, _encode(reflection)),
            )
        return reflection

    def _revise(
        self, db: sqlite3.Connection, knowledge_id: str, scope: dict[str, str],
        reference: dict, alternative: dict, direction: str,
    ) -> dict:
        previous = db.execute(
            "SELECT payload FROM knowledge_revisions WHERE knowledge_id = ? ORDER BY version DESC LIMIT 1",
            (knowledge_id,),
        ).fetchone()
        knowledge = json.loads(previous[0]) if previous else {
            "knowledge_id": knowledge_id, "scope": scope, "reference": reference, "alternative": alternative,
            "version": 0, "observations": {},
        }
        observations = knowledge["observations"]
        observations[direction] = observations.get(direction, 0) + 1
        knowledge.update(version=knowledge["version"] + 1, status="verified" if len(observations) == 1 else "conflicted")
        db.execute(
            "INSERT INTO knowledge_revisions VALUES (?, ?, ?, ?, ?)",
            (knowledge_id, knowledge["version"], _encode(scope), knowledge["status"], _encode(knowledge)),
        )
        return knowledge

    def query(self, scene: SceneState, *, limit: int = 5) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer between 1 and 20")
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT k.payload FROM knowledge_revisions k
                   WHERE k.scope = ? AND k.status = 'verified'
                   AND k.version = (SELECT MAX(version) FROM knowledge_revisions WHERE knowledge_id = k.knowledge_id)
                   ORDER BY k.version DESC, k.knowledge_id LIMIT ?""",
                (_encode(_scope(scene)), limit),
            ).fetchall()
        return [json.loads(payload) for payload, in rows]

    def explain(self, identifier: str, *, version: int | None = None) -> dict:
        with closing(self._connect()) as db:
            if identifier.startswith("reflection_"):
                if version is not None:
                    raise ValueError("version applies to a knowledge ID")
                row = db.execute("SELECT payload FROM reflections WHERE reflection_id = ?", (identifier,)).fetchone()
                if row is None:
                    raise ValueError(f"Unknown reflection ID: {identifier}")
                return json.loads(row[0])

            row = db.execute(
                """SELECT payload, version FROM knowledge_revisions WHERE knowledge_id = ?
                   AND version = COALESCE(?, version) ORDER BY version DESC LIMIT 1""",
                (identifier, version),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown knowledge ID or version: {identifier}")
            evidence = db.execute(
                """SELECT payload FROM reflections WHERE knowledge_id = ? AND version <= ?
                   ORDER BY version DESC LIMIT 5""",
                (identifier, row[1]),
            ).fetchall()
        return {**json.loads(row[0]), "recent_evidence": [json.loads(payload) for payload, in evidence]}
