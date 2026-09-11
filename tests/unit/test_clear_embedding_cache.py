from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.clear_embedding_cache import clear_embedding_cache


def test_clear_embedding_cache_deletes_only_query_cache(tmp_path: Path) -> None:
    db_path = tmp_path / "pulsar.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE query_cache (query TEXT PRIMARY KEY, dense_vector BLOB NOT NULL)")
        connection.executemany(
            "INSERT INTO query_cache (query, dense_vector) VALUES (?, ?)",
            [("query-1", "[1.0]"), ("query-2", "[2.0]")],
        )
        connection.execute("CREATE TABLE videos (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO videos (id) VALUES (1)")

    assert clear_embedding_cache(db_path) == 2

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1


def test_clear_embedding_cache_rejects_database_without_cache_table(tmp_path: Path) -> None:
    db_path = tmp_path / "pulsar.db"
    sqlite3.connect(db_path).close()

    with pytest.raises(RuntimeError, match="query_cache does not exist"):
        clear_embedding_cache(db_path)
