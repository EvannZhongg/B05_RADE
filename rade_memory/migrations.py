"""Explicit, atomic initialization of the domain-memory schema."""

import sqlite3
from importlib.resources import files
from pathlib import Path

from .database import connect_database


SCHEMA_VERSION = 1


def _read_schema() -> str:
    return files("rade_memory").joinpath("schema.sql").read_text(encoding="utf-8")


def _statements(script: str):
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete memory schema SQL")


def initialize_database(path: Path) -> None:
    """Add memory tables to a new or existing knowledge.sqlite3 file.

    Existing reflection tables remain under KnowledgeService's lazy setup.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if exists:
            row = connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
            if row is None or row["version"] != SCHEMA_VERSION:
                raise ValueError("Unsupported domain-memory schema version")
        else:
            for statement in _statements(_read_schema()):
                connection.execute(statement)
            connection.execute("INSERT INTO schema_version (id, version) VALUES (1, ?)", (SCHEMA_VERSION,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def current_schema_version(path: Path) -> int | None:
    """Read the initialized version without creating a missing database."""
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if row is None:
            return None
        result = connection.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        return result[0] if result else None
    finally:
        connection.close()
