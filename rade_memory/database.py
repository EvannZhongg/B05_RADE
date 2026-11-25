"""Small SQLite connection and transaction helpers for domain memory."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect_database(path: Path) -> sqlite3.Connection:
    """Open an explicitly requested database without running migrations."""
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def transaction(path: Path) -> Iterator[sqlite3.Connection]:
    """Commit a write unit atomically and always close its connection."""
    connection = connect_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
