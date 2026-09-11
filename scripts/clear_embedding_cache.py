"""Clear cached query embeddings from the Pulsar SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_sqlite_settings


def clear_embedding_cache(db_path: Path) -> int:
    """Delete only query embedding cache rows and return the deleted count."""
    with sqlite3.connect(db_path, timeout=30.0) as connection:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'query_cache'"
        ).fetchone()
        if table_exists is None:
            raise RuntimeError(f"Table query_cache does not exist in {db_path}")

        count = int(connection.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0])
        connection.execute("DELETE FROM query_cache")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db_path = get_sqlite_settings().db_path

    if not args.yes:
        answer = input(f"Clear all cached query embeddings from {db_path}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled; the cache was not changed.")
            return 0

    try:
        deleted = clear_embedding_cache(db_path)
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(f"Failed to clear embedding cache: {error}", file=sys.stderr)
        return 1

    print(f"Cleared {deleted} cached query embedding(s) from {db_path}.")
    print("Restart Pulsar separately if the in-memory L1 cache must also be cleared.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
