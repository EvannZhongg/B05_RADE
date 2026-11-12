import json
from pathlib import Path

import pytest

from agent_runtime.scene_validation import extract_scene_features


SCENES = (
    Path(__file__).resolve().parents[1]
    / "docs" / "反思式决策赛题信息" / "赛题自测场景"
)


@pytest.mark.parametrize(
    ("filename", "rows", "columns"),
    [
        ("scene1_0_50_20.json", 50, 20),
        ("scene1_1_40_30.json", 40, 30),
        ("scene1_2_50_20.json", 50, 20),
        ("scene1_3_50_20.json", 50, 20),
        ("scene1_4_50_20.json", 50, 20),
        ("scene2_0_500_200.json", 500, 200),
        ("scene2_1_400_300.json", 400, 300),
        ("scene2_2_500_200.json", 500, 200),
        ("scene2_3_500_200.json", 500, 200),
        ("scene2_4_500_200.json", 500, 200),
    ],
)
def test_contest_fixture_dimensions(filename, rows, columns):
    text = (SCENES / filename).read_text(encoding="utf-8")
    features = extract_scene_features(text)
    assert features["W"]["count"] == rows
    assert features["T"]["count"] == columns
    assert 0 < features["P"]["positive_fraction"] < 1
    assert 0 < features["V"]["top_10_percent_value_share"] <= 1


def test_extracts_candidate_and_probability_distributions_and_unreachable_value():
    features = extract_scene_features(
        '\ufeff{"P":[[0.25,0,1,0],[0.5,0,0,0],[0,0,0,0]],"v":[8,12,8,1.5]}'
    )
    assert features == {
        "T": {
            "count": 4,
            "tasks_without_candidates": 2,
            "tasks_with_one_candidate": 1,
            "candidate_workers_per_task": {"p10": 0, "median": 0.5, "p90": 2},
        },
        "W": {
            "count": 3,
            "workers_without_candidates": 1,
            "candidate_tasks_per_worker": {"p10": 0, "median": 1, "p90": 2},
        },
        "P": {
            "positive_fraction": 0.25,
            "positive_probability_mean": pytest.approx(7 / 12),
            "positive_probability_median": 0.5,
            "best_probability_per_task": {"min": 0, "p10": 0, "median": 0.25, "p90": 1},
        },
        "V": {
            "total_value": 29.5,
            "unreachable_value": 13.5,
            "value_distribution": {"p10": 1.5, "median": 8, "p90": 12},
            "top_10_percent_value_share": 0.40678,
        },
    }


def test_all_zero_probabilities_and_value():
    features = extract_scene_features('{"P":[[0]],"v":[0]}')
    assert features["T"]["tasks_without_candidates"] == 1
    assert features["W"]["workers_without_candidates"] == 1
    assert features["P"] == {
        "positive_fraction": 0,
        "positive_probability_mean": None,
        "positive_probability_median": None,
        "best_probability_per_task": {"min": 0, "p10": 0, "median": 0, "p90": 0},
    }
    assert features["V"]["total_value"] == 0
    assert features["V"]["top_10_percent_value_share"] is None


@pytest.mark.parametrize("repeat", [1, 20])
def test_top_decile_share_is_stable_under_replication(repeat):
    values = list(range(1, 11)) * repeat
    features = extract_scene_features(json.dumps({"P": [[1] * len(values)], "v": values}))
    assert features["V"]["total_value"] == 55 * repeat
    assert features["V"]["value_distribution"] == {"p10": 1, "median": 5.5, "p90": 9}
    assert features["V"]["top_10_percent_value_share"] == 0.181818


def test_top_decile_rounds_task_count_up():
    values = [1] * 9 + [6, 9]
    features = extract_scene_features(json.dumps({"P": [[1] * len(values)], "v": values}))
    assert features["V"]["top_10_percent_value_share"] == 0.625


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('{"P":', "Expecting value"),
        ("[]", "scene must be a JSON object"),
        ('{"P": [[0]], "V": [1]}', "v must be a non-empty array"),
        ('{"P": [], "v": [1]}', "P must be a non-empty matrix"),
        ('{"P": [[0], [0, 1]], "v": [1]}', r"P\[1\] must be an array of length 1"),
        ('{"P": [0], "v": [1]}', r"P\[0\] must be an array of length 1"),
        ('{"P": [[0]], "v": []}', "v must be a non-empty array"),
        ('{"P": [[0]], "v": [1, 2]}', r"P\[0\] must be an array of length 2"),
        ('{"P": [[-0.1]], "v": [1]}', r"P\[0\]\[0\] must be a number in \[0, 1\]"),
        ('{"P": [[1.1]], "v": [1]}', r"P\[0\]\[0\] must be a number in \[0, 1\]"),
        ('{"P": [[false]], "v": [1]}', r"P\[0\]\[0\] must be a number in \[0, 1\]"),
        ('{"P": [[0]], "v": [true]}', r"v\[0\] must be a finite number"),
        ('{"P": [[0]], "v": [1e400]}', r"v\[0\] must be a finite number"),
        ('{"P": [[NaN]], "v": [1]}', "NaN is not a valid JSON value"),
    ],
)
def test_rejects_invalid_scene(text, error):
    with pytest.raises(ValueError, match=error):
        extract_scene_features(text)
