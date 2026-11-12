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
    assert len(features["V"]["top_tasks_by_value"]) == 5


def test_extracts_axis_statistics_and_links_high_value_tasks_to_probabilities():
    features = extract_scene_features(
        '\ufeff{"P":[[0.25,0,1,0],[0.5,0,0,0],[0,0,0,0]],"v":[8,12,8,1.5]}'
    )
    assert features == {
        "T": {
            "count": 4,
            "without_positive_probability": 2,
            "min_candidate_workers": 0,
            "max_candidate_workers": 2,
            "mean_candidate_workers": 0.75,
        },
        "W": {
            "count": 3,
            "without_positive_probability": 1,
            "min_candidate_tasks": 0,
            "max_candidate_tasks": 2,
            "mean_candidate_tasks": 1.0,
        },
        "P": {"positive_fraction": 0.25, "mean_probability": 0.145833, "max_probability": 1},
        "V": {
            "min_value": 1.5,
            "max_value": 12,
            "top_tasks_by_value": [
                {"task_id": 2, "value": 12, "candidate_workers": 0, "best_probability": 0},
                {"task_id": 1, "value": 8, "candidate_workers": 2, "best_probability": 0.5},
                {"task_id": 3, "value": 8, "candidate_workers": 1, "best_probability": 1},
                {"task_id": 4, "value": 1.5, "candidate_workers": 0, "best_probability": 0},
            ],
        },
    }


def test_all_zero_probabilities():
    features = extract_scene_features('{"P":[[0]],"v":[2]}')
    assert features["T"]["without_positive_probability"] == 1
    assert features["W"]["without_positive_probability"] == 1
    assert features["P"] == {"positive_fraction": 0, "mean_probability": 0, "max_probability": 0}


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
