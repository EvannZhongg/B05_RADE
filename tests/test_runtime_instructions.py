from agent_runtime.instructions import load_runtime_instructions


def test_runtime_instructions_follow_configuration_file(tmp_path):
    initial = load_runtime_instructions(tmp_path)
    assert not initial

    path = tmp_path / "AGENTS.md"
    path.write_text("first rule", encoding="utf-8")
    first = load_runtime_instructions(tmp_path)
    assert [(doc.label, doc.content) for doc in first.documents] == [
        (".RADE/AGENTS.md", "first rule"),
    ]
    assert first.fingerprint != initial.fingerprint

    path.write_text("second rule", encoding="utf-8")
    assert load_runtime_instructions(tmp_path).fingerprint != first.fingerprint
