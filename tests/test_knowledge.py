from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256

import pytest

from rade_tasks.evaluation import evaluate
from rade_tasks.experiments import ExperimentRecord
from rade_tasks.knowledge import KnowledgeService, ReflectionProposal, ReflectionSource
from rade_tasks.scene import SceneState


def comparison(values: str, session: str):
    text = '{"P":[[0.8,0.7],[1,0]],"v":' + values + "}"
    scene = SceneState(text)
    source = ReflectionSource(session, f"{session}-task", "scene1_1.json", sha256(text.encode()).hexdigest(),
                              "scenes/snapshot/scene1_1.json")
    records = {}
    for config, allocation in (("default", [0, 0]), ("reverse", [1, 0])):
        record = ExperimentRecord(
            experiment_id=f"{session}-{config}", strategy_id="greedy", config_id=config,
            version="1.0", seed=0, timeout_seconds=30, scene_digest=source.scene_digest,
            task_id=source.task_id, status="completed", elapsed_seconds=0.01, solver_elapsed_seconds=0.01,
            valid=True, metrics=evaluate(scene.snapshot, allocation), diagnostic=None, result_ref=f"{config}.json",
            allocation_digest=f"{config}-digest",
        )
        records[record.experiment_id] = record, allocation
    proposal = ReflectionProposal(
        "Trying reverse order could distribute workers across both tasks.",
        "Reverse order improved measured expected value.",
        f"{session}-default", f"{session}-reverse", "improved",
    )
    return proposal, source, scene, records


def test_verified_reflections_are_persisted_deduplicated_and_versioned(tmp_path):
    database = tmp_path / "knowledge.sqlite3"
    service = KnowledgeService(database)
    proposal, source, scene, records = comparison("[10,10]", "first")
    first = service.propose(proposal, source, scene, records.__getitem__)
    assert first["status"] == "verified"
    assert first["knowledge_version"] == 1
    assert first["source"]["task_id"] == source.task_id
    assert first["evidence"][1]["expected_value"] == 17
    assert service.propose(proposal, source, scene, records.__getitem__) == first

    # Another session can report the same observation in the opposite order.
    service = KnowledgeService(database)
    proposal, source, scene, records = comparison("[10,10]", "duplicate")
    proposal = replace(proposal, experiment_id=proposal.reference_experiment_id,
                       reference_experiment_id=proposal.experiment_id, expected_value_effect="regressed")
    duplicate = service.propose(proposal, source, scene, records.__getitem__)
    assert duplicate["duplicate_of"] == first["reflection_id"]
    assert service.explain(duplicate["reflection_id"]) == duplicate

    proposal, source, scene, records = comparison("[10,9]", "second")
    knowledge_id = first["knowledge_id"]
    entry, = service.query(scene)
    assert entry["knowledge_id"] == knowledge_id
    assert entry["version"] == 1
    assert service.explain(knowledge_id)["recent_evidence"] == [first]
    second = service.propose(proposal, source, scene, records.__getitem__)
    assert second["knowledge_id"] == knowledge_id
    assert second["knowledge_version"] == 2
    assert service.explain(knowledge_id)["observations"] == {"improved": 2}
    assert service.explain(knowledge_id, version=1)["recent_evidence"] == [first]

    proposal, source, scene, records = comparison("[10,0]", "conflict")
    conflicting = service.propose(
        replace(proposal, expected_value_effect="unchanged"), source, scene, records.__getitem__,
    )
    assert conflicting["status"] == "verified"
    assert service.query(scene) == []
    latest = service.explain(knowledge_id)
    assert latest["status"] == "conflicted"
    assert latest["version"] == 3
    assert latest["observations"] == {"improved": 2, "unchanged": 1}
    assert latest["recent_evidence"] == [conflicting, second, first]
    assert service.explain(knowledge_id, version=2)["status"] == "verified"


@pytest.mark.parametrize(("record_changes", "allocation", "claim", "reason"), [
    ({"task_id": "other"}, [1, 0], "improved", "current scene task"),
    ({"scene_digest": "other"}, [1, 0], "improved", "current scene task"),
    ({"status": "running"}, [1, 0], "improved", "completed, valid"),
    ({}, None, "improved", "completed, valid"),
    ({}, [True, 0], "improved", "task index"),
    ({"metrics": {"expected_value": 100}}, [1, 0], "improved", "inconsistent"),
    ({}, [1, 0], "regressed", "measured effect improved"),
])
def test_invalid_evidence_never_promotes_knowledge(tmp_path, record_changes, allocation, claim, reason):
    service = KnowledgeService(tmp_path / "knowledge.sqlite3")
    proposal, source, scene, records = comparison("[10,10]", "run")
    record, _ = records[proposal.experiment_id]
    records[record.experiment_id] = replace(record, **record_changes), allocation
    result = service.propose(replace(proposal, expected_value_effect=claim), source, scene, records.__getitem__)
    assert result["status"] == "rejected"
    assert reason in result["reason"]
    assert service.explain(result["reflection_id"]) == result
    assert service.query(scene) == []


def test_concurrent_writers_create_one_revision_per_observation(tmp_path):
    database = tmp_path / "knowledge.sqlite3"

    def submit(index):
        proposal, source, scene, records = comparison("[10,10]", f"run-{index}")
        return KnowledgeService(database).propose(proposal, source, scene, records.__getitem__)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(8)))
    assert len({result["knowledge_id"] for result in results}) == 1
    first, = (result for result in results if "duplicate_of" not in result)
    assert all(result["knowledge_version"] == 1 for result in results)
    assert KnowledgeService(database).explain(first["knowledge_id"])["observations"] == {"improved": 1}
