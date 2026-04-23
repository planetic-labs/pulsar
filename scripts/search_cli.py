from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_app_settings, get_sqlite_settings
from app.db import db_connection, init_db
from app.search import format_timestamp, hybrid_search


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search indexed transcript chunks")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_app_settings()
    pg_settings = get_sqlite_settings()

    with db_connection(pg_settings) as connection:
        init_db(connection)
        results = hybrid_search(
            connection,
            args.query,
            limit=args.limit or settings.results_limit,
        )

    for item in results:
        print(
            f"[{item.match_type}] {item.title} "
            f"{format_timestamp(item.start_sec)}-{format_timestamp(item.end_sec)} "
            f"score={item.combined_score:.3f}"
        )
        print(item.text)
        print()


if __name__ == "__main__":
    main()
