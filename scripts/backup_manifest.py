#!/usr/bin/env python3
"""Create and validate a fail-closed manifest for a Pulsar SQLite backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.chunking import CHUNKING_ALGORITHM_VERSION, get_chunking_config_hash, get_chunking_manifest

MANIFEST_VERSION = 1
REQUIRED_TABLES = {"chunks", "folders", "tasks", "videos"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def directory_sha256(path: Path) -> tuple[str, int, int]:
    """Hash relative names and bytes so a physical Manticore backup is self-verifying."""
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for item in sorted((entry for entry in path.rglob("*") if entry.is_file()), key=lambda entry: entry.as_posix()):
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        size = item.stat().st_size
        file_count += 1
        total_bytes += size
        with item.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    if not file_count:
        raise ValueError(f"Manticore backup directory is empty: {path}")
    return digest.hexdigest(), file_count, total_bytes


def inspect_database(path: Path) -> dict[str, Any]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(REQUIRED_TABLES - tables)
        if integrity != ["ok"] or missing:
            raise ValueError(f"invalid SQLite backup: integrity={integrity}, missing_tables={missing}")
        stats = {
            "chunks": int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
            "folders": int(conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]),
            "tasks": int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]),
            "videos": int(conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]),
        }
        generation = None
        if "index_generations" in tables:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM index_generations WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            generation = dict(row) if row else None
    return {"integrity": integrity, "stats": stats, "active_generation": generation}


def create_manifest(
    db_path: Path,
    manticore_backup: Path,
    manticore_count: int,
    environment: str,
) -> dict[str, Any]:
    database = inspect_database(db_path)
    if manticore_count != database["stats"]["chunks"]:
        raise ValueError(f"SQLite/Manticore chunk count mismatch: {database['stats']['chunks']} != {manticore_count}")
    manticore_hash, manticore_files, manticore_bytes = directory_sha256(manticore_backup)
    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "environment": environment,
        "sqlite": {
            "filename": db_path.name,
            "sha256": file_sha256(db_path),
            **database,
        },
        "chunking": {
            "version": CHUNKING_ALGORITHM_VERSION,
            "config_hash": get_chunking_config_hash(),
            "settings": get_chunking_manifest(),
        },
        "embedding": {
            "provider": os.getenv("EMBEDDING_PROVIDER", "custom"),
            "model": os.getenv("EMBEDDING_MODEL_ID", "BAAI/bge-m3"),
            "dimension": int(os.getenv("EMBEDDING_DIMENSION", "1024")),
        },
        "manticore": {
            "backup_mode": "physical_consistent",
            "directory": manticore_backup.name,
            "sha256": manticore_hash,
            "file_count": manticore_files,
            "bytes": manticore_bytes,
            "chunks": manticore_count,
        },
    }


def validate_manifest(
    db_path: Path,
    manifest_path: Path,
    manticore_backup: Path,
    environment: str | None,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        errors.append(f"unsupported manifest_version={manifest.get('manifest_version')}")
    if environment and manifest.get("environment") != environment:
        errors.append(f"environment mismatch: backup={manifest.get('environment')} target={environment}")
    expected_hash = manifest.get("sqlite", {}).get("sha256")
    actual_hash = file_sha256(db_path)
    if expected_hash != actual_hash:
        errors.append(f"SQLite SHA-256 mismatch: expected={expected_hash} actual={actual_hash}")
    database = inspect_database(db_path)
    expected_stats = manifest.get("sqlite", {}).get("stats")
    if expected_stats != database["stats"]:
        errors.append(f"SQLite row-count mismatch: expected={expected_stats} actual={database['stats']}")
    actual_manticore_hash, actual_files, actual_bytes = directory_sha256(manticore_backup)
    expected_manticore = manifest.get("manticore", {})
    if expected_manticore.get("chunks") != database["stats"]["chunks"]:
        errors.append(
            "SQLite/Manticore manifest count mismatch: "
            f"sqlite={database['stats']['chunks']} manticore={expected_manticore.get('chunks')}"
        )
    if expected_manticore.get("sha256") != actual_manticore_hash:
        errors.append("Manticore backup SHA-256 mismatch")
    if expected_manticore.get("file_count") != actual_files or expected_manticore.get("bytes") != actual_bytes:
        errors.append(
            "Manticore backup size mismatch: "
            f"expected={expected_manticore.get('file_count')}/{expected_manticore.get('bytes')} "
            f"actual={actual_files}/{actual_bytes}"
        )
    if errors:
        raise ValueError("; ".join(errors))
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--db", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--manticore-backup", type=Path, required=True)
    create.add_argument("--manticore-count", type=int, required=True)
    create.add_argument("--environment", choices=["dev", "prod"], required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--db", type=Path, required=True)
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--manticore-backup", type=Path, required=True)
    validate.add_argument("--environment", choices=["dev", "prod"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "create":
            manifest = create_manifest(args.db, args.manticore_backup, args.manticore_count, args.environment)
            args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        else:
            manifest = validate_manifest(args.db, args.manifest, args.manticore_backup, args.environment)
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"backup manifest validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
