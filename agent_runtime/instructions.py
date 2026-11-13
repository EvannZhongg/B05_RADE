from hashlib import sha256
from pathlib import Path

from agent_core.runtime_instructions import (
    RuntimeInstruction,
    RuntimeInstructions,
)


def load_runtime_instructions(config_directory: Path) -> RuntimeInstructions:
    path = config_directory / "AGENTS.md"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return RuntimeInstructions(documents=(), fingerprint="")
    return RuntimeInstructions(
        documents=(RuntimeInstruction(".RADE/AGENTS.md", raw.decode("utf-8").strip()),),
        fingerprint=sha256(raw).hexdigest(),
    )


__all__ = ["load_runtime_instructions"]
