import sqlite3

import pytest

from scripts.verify_integrity_readonly import readonly_connection, validate_readonly_manticore_sql


def test_readonly_connection_allows_reads_and_rejects_writes(tmp_path):
    db_path = tmp_path / "audit.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO records (value) VALUES ('unchanged')")

    with readonly_connection(db_path) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("SELECT value FROM records").fetchone()[0] == "unchanged"
        with pytest.raises(sqlite3.OperationalError, match=r"readonly|read-only"):
            conn.execute("UPDATE records SET value = 'changed'")

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT value FROM records").fetchone()[0] == "unchanged"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM chunks",
        "  SHOW TABLES",
        "DESCRIBE chunks",
        "DESC chunks",
    ],
)
def test_manticore_guard_allows_read_only_statements(sql):
    validate_readonly_manticore_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "INSERT INTO chunks VALUES (1)",
        "REPLACE INTO chunks VALUES (1)",
        "UPDATE chunks SET text = 'changed'",
        "DELETE FROM chunks WHERE id = 1",
        "TRUNCATE TABLE chunks",
        "CREATE TABLE other (id bigint)",
        "DROP TABLE chunks",
        "ALTER TABLE chunks ADD COLUMN extra text",
        "CALL PQ('chunks', 'query')",
        "SELECT id FROM chunks; DELETE FROM chunks",
    ],
)
def test_manticore_guard_rejects_mutating_or_unknown_statements(sql):
    with pytest.raises(ValueError, match=r"[Rr]ead-only"):
        validate_readonly_manticore_sql(sql)
