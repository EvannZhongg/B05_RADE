import re
from pathlib import Path


def default_runs_directory() -> Path:
    return Path.cwd() / ".RADE" / "runs"


def session_directory(directory: Path, session_id: str) -> Path:
    validate_session_id(session_id)
    path = directory / session_id
    if path.resolve() != path:
        raise ValueError(f"Invalid session directory: {session_id!r}")
    return path


def session_log_path(directory: Path, session_id: str) -> Path:
    return session_directory(directory, session_id) / f"{session_id}.jsonl"


def validate_session_id(session_id: str) -> None:
    if (
        not isinstance(session_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id)
        or session_id.upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError(f"Invalid session id: {session_id!r}")


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)),
}
