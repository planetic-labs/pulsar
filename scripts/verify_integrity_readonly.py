"""Read-only integrity audit for Pulsar.

Unlike verify_integrity.py, this script never repairs data, deletes files,
updates SQLite, changes Manticore, or queues background tasks. It only reads
the current state and prints a detailed report.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.chunking import chunk_from_utterances
from app.config import get_app_settings, get_embedding_settings, get_manticore_settings, get_sqlite_settings
from app.manticore import get_manticore_client


@dataclass(frozen=True)
class Issue:
    severity: str
    category: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@contextmanager
def readonly_connection(db_path: Path) -> Generator[sqlite3.Connection]:
    """Open SQLite in OS-level read-only mode and reject SQL mutations."""
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        yield conn
    finally:
        conn.close()


def validate_readonly_manticore_sql(sql: str) -> None:
    """Reject any Manticore statement that is not explicitly read-only."""
    stripped = sql.strip()
    if ";" in stripped.rstrip(";"):
        raise ValueError("Multiple Manticore statements are rejected in read-only mode")
    first_token = stripped.split(None, 1)[0].upper() if stripped else ""
    if first_token not in {"SELECT", "SHOW", "DESCRIBE", "DESC"}:
        raise ValueError(f"Non-read-only Manticore statement rejected: {first_token or '<empty>'}")


def print_progress(message: str) -> None:
    """Keep progress separate so --json remains machine-readable on stdout."""
    print(message, file=sys.stderr, flush=True)


class ReadonlyIntegrityChecker:
    """Collect a detailed integrity report without changing system state."""

    def __init__(self, *, max_details: int = 50, vector_sample_size: int = 100) -> None:
        self.app_settings = get_app_settings()
        self.sqlite_settings = get_sqlite_settings()
        self.manticore_settings = get_manticore_settings()
        self.embedding_settings = get_embedding_settings()
        self.manticore = get_manticore_client()
        self.max_details = max_details
        self.vector_sample_size = vector_sample_size
        self.issues: list[Issue] = []
        self.stats: dict[str, Any] = {}

    def add_issue(self, severity: str, category: str, message: str, **context: Any) -> None:
        self.issues.append(Issue(severity=severity, category=category, message=message, context=context))

    def manticore_select(self, sql: str) -> list[Any]:
        validate_readonly_manticore_sql(sql)
        return self.manticore._execute_sql(sql)

    @staticmethod
    def manticore_rows(response: list[Any]) -> list[dict[str, Any]]:
        if not response:
            return []
        payload = response[0]
        data = payload.get("data", [])
        columns_meta = payload.get("columns", [])
        columns = [next(iter(column.keys())) for column in columns_meta] if isinstance(columns_meta, list) else []
        return [row if isinstance(row, dict) else dict(zip(columns, row, strict=False)) for row in data]

    def check_sqlite_health(self) -> None:
        print_progress("[1/7] SQLite: structural integrity and queue state")
        with readonly_connection(self.sqlite_settings.db_path) as conn:
            integrity_rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
            self.stats["sqlite_integrity"] = integrity_rows
            if integrity_rows != ["ok"]:
                for result in integrity_rows:
                    self.add_issue("critical", "sqlite_integrity", result)

            foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            self.stats["sqlite_foreign_key_violations"] = len(foreign_key_rows)
            for row in foreign_key_rows:
                self.add_issue(
                    "error",
                    "sqlite_foreign_key",
                    f"Foreign-key violation in table {row[0]} at rowid {row[1]}",
                    table=row[0],
                    rowid=row[1],
                    parent=row[2],
                    foreign_key_index=row[3],
                )

            task_rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status ORDER BY status"
            ).fetchall()
            task_counts = {row["status"]: row["cnt"] for row in task_rows}
            self.stats["tasks_by_status"] = task_counts
            active_count = task_counts.get("pending", 0) + task_counts.get("running", 0)
            self.stats["active_tasks"] = active_count
            if active_count:
                self.add_issue(
                    "warning",
                    "worker_active",
                    f"There are {active_count} pending or running tasks; audit continued in read-only mode",
                )

    def check_filesystem_and_sqlite(self) -> None:
        print_progress("[2/7] Filesystem: transcripts versus SQLite")
        expected_raw: set[Path] = set()
        expected_normalized: set[Path] = set()

        with readonly_connection(self.sqlite_settings.db_path) as conn:
            videos = conn.execute(
                """
                SELECT id AS video_id, source_file_id, title, is_short
                FROM videos
                WHERE original_id IS NULL AND is_silent = 0
                ORDER BY id
                """
            ).fetchall()
            self.stats["original_non_silent_videos"] = len(videos)

            for position, video in enumerate(videos, start=1):
                if position == 1 or position % 500 == 0 or position == len(videos):
                    print_progress(f"      transcripts checked: {position}/{len(videos)}")
                file_id = video["source_file_id"]
                if not file_id:
                    self.add_issue(
                        "error",
                        "missing_source_file_id",
                        f"Video {video['video_id']} has no source_file_id",
                        video_id=video["video_id"],
                        title=video["title"],
                    )
                    continue

                raw_path = self.app_settings.get_raw_transcript_path(file_id)
                normalized_path = self.app_settings.get_normalized_transcript_path(file_id)
                expected_raw.add(raw_path.resolve())
                expected_normalized.add(normalized_path.resolve())

                if not raw_path.is_file():
                    self.add_issue(
                        "error",
                        "missing_raw",
                        f"Missing RAW transcript: {raw_path}",
                        video_id=video["video_id"],
                        source_file_id=file_id,
                    )
                else:
                    try:
                        with gzip.open(raw_path, "rt", encoding="utf-8") as file:
                            json.load(file)
                    except Exception as exc:
                        self.add_issue(
                            "error",
                            "corrupt_raw",
                            f"Invalid RAW transcript: {raw_path}: {exc}",
                            video_id=video["video_id"],
                            source_file_id=file_id,
                        )

                if not normalized_path.is_file():
                    self.add_issue(
                        "error",
                        "missing_normalized",
                        f"Missing normalized transcript: {normalized_path}",
                        video_id=video["video_id"],
                        source_file_id=file_id,
                    )
                    continue

                try:
                    with gzip.open(normalized_path, "rt", encoding="utf-8") as file:
                        normalized_data = json.load(file)
                except Exception as exc:
                    self.add_issue(
                        "error",
                        "corrupt_normalized",
                        f"Invalid normalized transcript: {normalized_path}: {exc}",
                        video_id=video["video_id"],
                        source_file_id=file_id,
                    )
                    continue

                transcript_parts = normalized_data.get("utterances") or normalized_data.get("chunks") or []
                try:
                    expected_chunks = chunk_from_utterances(transcript_parts, single_chunk=bool(video["is_short"]))
                except Exception as exc:
                    self.add_issue(
                        "error",
                        "chunk_generation",
                        f"Cannot derive chunks from normalized transcript for video {video['video_id']}: {exc}",
                        video_id=video["video_id"],
                        path=str(normalized_path),
                    )
                    continue

                db_chunks = conn.execute(
                    "SELECT chunk_index, text FROM chunks WHERE video_id = ? ORDER BY chunk_index",
                    (video["video_id"],),
                ).fetchall()
                if len(db_chunks) != len(expected_chunks):
                    self.add_issue(
                        "error",
                        "chunk_count_mismatch",
                        (
                            f"Video {video['video_id']} chunk count differs: "
                            f"SQLite={len(db_chunks)}, normalized={len(expected_chunks)}"
                        ),
                        video_id=video["video_id"],
                        title=video["title"],
                        sqlite_count=len(db_chunks),
                        normalized_count=len(expected_chunks),
                    )

                if db_chunks and transcript_parts:
                    json_text = str(transcript_parts[0].get("text", "")).strip()
                    sqlite_text = str(db_chunks[0]["text"] or "").strip()
                    if json_text and not sqlite_text.startswith(json_text):
                        self.add_issue(
                            "warning",
                            "first_chunk_text_mismatch",
                            f"Video {video['video_id']} first chunk text differs between SQLite and normalized JSON",
                            video_id=video["video_id"],
                            sqlite_preview=sqlite_text[:160],
                            normalized_preview=json_text[:160],
                        )

        raw_on_disk = {path.resolve() for path in self.app_settings.raw_transcripts_dir.glob("**/*.json.gz")}
        normalized_on_disk = {
            path.resolve() for path in self.app_settings.normalized_transcripts_dir.glob("**/*.json.gz")
        }
        orphan_raw = sorted(raw_on_disk - expected_raw)
        orphan_normalized = sorted(normalized_on_disk - expected_normalized)

        self.stats.update(
            {
                "raw_expected": len(expected_raw),
                "raw_on_disk": len(raw_on_disk),
                "raw_orphans": len(orphan_raw),
                "normalized_expected": len(expected_normalized),
                "normalized_on_disk": len(normalized_on_disk),
                "normalized_orphans": len(orphan_normalized),
            }
        )
        for path in orphan_raw:
            self.add_issue(
                "warning",
                "orphan_raw",
                f"RAW transcript is not referenced by SQLite: {path}",
                path=str(path),
            )
        for path in orphan_normalized:
            self.add_issue(
                "warning",
                "orphan_normalized",
                f"Normalized transcript is not referenced by SQLite: {path}",
                path=str(path),
            )

    def load_manticore_chunks(self) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        last_id = 0
        batch_size = 5000
        while True:
            sql = (
                f"SELECT id, video_id, chunk_index, text FROM {self.manticore_settings.table_name} "
                f"WHERE id > {last_id} ORDER BY id ASC LIMIT {batch_size} "
                f"OPTION max_matches={batch_size}"
            )
            rows = self.manticore_rows(self.manticore_select(sql))
            if not rows:
                break
            for row in rows:
                chunk_id = int(row["id"])
                result[chunk_id] = {
                    "video_id": row.get("video_id"),
                    "chunk_index": row.get("chunk_index"),
                    "text": row.get("text") or "",
                }
                last_id = max(last_id, chunk_id)
            if len(rows) < batch_size:
                break
        return result

    def check_sqlite_vs_manticore(self) -> set[int]:
        print_progress("[3/7] Search index: SQLite versus Manticore")
        with readonly_connection(self.sqlite_settings.db_path) as conn:
            sqlite_rows = conn.execute("SELECT id, video_id, chunk_index, text FROM chunks ORDER BY id").fetchall()
            sqlite_chunks = {
                int(row["id"]): {
                    "video_id": row["video_id"],
                    "chunk_index": row["chunk_index"],
                    "text": row["text"] or "",
                }
                for row in sqlite_rows
            }

        try:
            manticore_chunks = self.load_manticore_chunks()
        except Exception as exc:
            self.add_issue("critical", "manticore_query", f"Cannot read Manticore index: {exc}")
            self.stats["sqlite_chunks"] = len(sqlite_chunks)
            self.stats["manticore_chunks"] = None
            return set()

        sqlite_ids = set(sqlite_chunks)
        manticore_ids = set(manticore_chunks)
        missing_ids = sorted(sqlite_ids - manticore_ids)
        orphan_ids = sorted(manticore_ids - sqlite_ids)
        self.stats.update(
            {
                "sqlite_chunks": len(sqlite_ids),
                "manticore_chunks": len(manticore_ids),
                "missing_in_manticore": len(missing_ids),
                "orphan_in_manticore": len(orphan_ids),
            }
        )

        for chunk_id in missing_ids:
            chunk = sqlite_chunks[chunk_id]
            self.add_issue(
                "error",
                "missing_in_manticore",
                f"SQLite chunk {chunk_id} is missing in Manticore",
                chunk_id=chunk_id,
                video_id=chunk["video_id"],
                chunk_index=chunk["chunk_index"],
            )
        for chunk_id in orphan_ids:
            chunk = manticore_chunks[chunk_id]
            self.add_issue(
                "error",
                "orphan_in_manticore",
                f"Manticore chunk {chunk_id} is missing in SQLite",
                chunk_id=chunk_id,
                video_id=chunk["video_id"],
                chunk_index=chunk["chunk_index"],
            )

        metadata_mismatches = 0
        text_mismatches = 0
        for chunk_id in sorted(sqlite_ids & manticore_ids):
            sqlite_chunk = sqlite_chunks[chunk_id]
            manticore_chunk = manticore_chunks[chunk_id]
            manticore_video_id = manticore_chunk["video_id"]
            if isinstance(manticore_video_id, str) and manticore_video_id.isdigit():
                manticore_video_id = int(manticore_video_id)
            manticore_chunk_index = manticore_chunk["chunk_index"]
            if isinstance(manticore_chunk_index, str) and manticore_chunk_index.isdigit():
                manticore_chunk_index = int(manticore_chunk_index)
            comparable_manticore = {
                "video_id": manticore_video_id,
                "chunk_index": manticore_chunk_index,
            }
            metadata_diff = {
                key: {"sqlite": sqlite_chunk[key], "manticore": comparable_manticore[key]}
                for key in ("video_id", "chunk_index")
                if sqlite_chunk[key] != comparable_manticore[key]
            }
            if metadata_diff:
                metadata_mismatches += 1
                self.add_issue(
                    "error",
                    "manticore_metadata_mismatch",
                    f"Chunk {chunk_id} metadata differs between SQLite and Manticore",
                    chunk_id=chunk_id,
                    differences=metadata_diff,
                )

            sqlite_text = str(sqlite_chunk["text"]).strip()
            manticore_text = str(manticore_chunk["text"]).strip()
            if sqlite_text != manticore_text:
                text_mismatches += 1
                self.add_issue(
                    "error",
                    "manticore_text_mismatch",
                    f"Chunk {chunk_id} text differs between SQLite and Manticore",
                    chunk_id=chunk_id,
                    sqlite_preview=sqlite_text[:160],
                    manticore_preview=manticore_text[:160],
                )

        self.stats["manticore_metadata_mismatches"] = metadata_mismatches
        self.stats["manticore_text_mismatches"] = text_mismatches
        return manticore_ids

    def check_vectors(self, manticore_ids: set[int]) -> None:
        print_progress("[4/7] Search index: deterministic vector sample")
        expected_dimension = self.embedding_settings.dimension or 1024
        self.stats["expected_vector_dimension"] = expected_dimension
        if not manticore_ids or self.vector_sample_size == 0:
            self.stats["vectors_checked"] = 0
            return

        ordered_ids = sorted(manticore_ids)
        sample_size = min(self.vector_sample_size, len(ordered_ids))
        step = max(1, len(ordered_ids) // sample_size)
        sample_ids = ordered_ids[::step][:sample_size]
        ids_sql = ",".join(str(chunk_id) for chunk_id in sample_ids)
        try:
            rows = self.manticore_rows(
                self.manticore_select(
                    f"SELECT id, vec FROM {self.manticore_settings.table_name} WHERE id IN ({ids_sql}) "
                    f"LIMIT {sample_size} OPTION max_matches={sample_size}"
                )
            )
        except Exception as exc:
            self.add_issue("error", "vector_query", f"Cannot read vectors from Manticore: {exc}")
            self.stats["vectors_checked"] = 0
            return

        returned_ids: set[int] = set()
        for row in rows:
            chunk_id = int(row["id"])
            returned_ids.add(chunk_id)
            value = row.get("vec")
            if isinstance(value, str):
                try:
                    vector = [float(item) for item in value.strip("[]").split(",") if item.strip()]
                except ValueError:
                    vector = []
            elif isinstance(value, list):
                vector = value
            else:
                vector = []

            if len(vector) != expected_dimension:
                self.add_issue(
                    "error",
                    "vector_dimension",
                    f"Chunk {chunk_id} vector dimension is {len(vector)}, expected {expected_dimension}",
                    chunk_id=chunk_id,
                    actual_dimension=len(vector),
                    expected_dimension=expected_dimension,
                )
            elif sum(float(component) ** 2 for component in vector) < 1e-6:
                self.add_issue("error", "zero_vector", f"Chunk {chunk_id} has a zero vector", chunk_id=chunk_id)

        for chunk_id in sorted(set(sample_ids) - returned_ids):
            self.add_issue(
                "error",
                "missing_sample_vector",
                f"Sampled chunk {chunk_id} was not returned by vector query",
                chunk_id=chunk_id,
            )
        self.stats["vectors_checked"] = len(rows)
        self.stats["vector_sample_requested"] = sample_size

    def check_relational_logic(self) -> None:
        print_progress("[5/7] SQLite: relational and duplication checks")
        with readonly_connection(self.sqlite_settings.db_path) as conn:
            checks = {
                "videos_without_chunks": conn.execute(
                    """
                    SELECT v.id, v.title
                    FROM videos v LEFT JOIN chunks c ON c.video_id = v.id
                    WHERE v.status IN ('completed', 'indexed_chunks_ready') AND v.is_silent = 0
                    GROUP BY v.id HAVING COUNT(c.id) = 0 ORDER BY v.id
                    """
                ).fetchall(),
                "duplicate_sources": conn.execute(
                    """
                    SELECT source_file_id, COUNT(*) AS cnt FROM videos
                    WHERE source_file_id IS NOT NULL AND source_file_id != ''
                    GROUP BY source_file_id HAVING COUNT(*) > 1 ORDER BY source_file_id
                    """
                ).fetchall(),
                "duplicate_videos_with_chunks": conn.execute(
                    """
                    SELECT v.id, v.title, v.original_id, COUNT(c.id) AS chunk_count
                    FROM videos v JOIN chunks c ON c.video_id = v.id
                    WHERE v.original_id IS NOT NULL GROUP BY v.id ORDER BY v.id
                    """
                ).fetchall(),
                "duplicate_md5": conn.execute(
                    """
                    SELECT md5_checksum, COUNT(*) AS cnt FROM videos
                    WHERE original_id IS NULL AND md5_checksum IS NOT NULL AND md5_checksum != ''
                    GROUP BY md5_checksum HAVING COUNT(*) > 1 ORDER BY md5_checksum
                    """
                ).fetchall(),
                "orphan_duplicates": conn.execute(
                    """
                    SELECT v.id, v.title, v.original_id
                    FROM videos v LEFT JOIN videos original ON original.id = v.original_id
                    WHERE v.original_id IS NOT NULL
                      AND (original.id IS NULL OR original.original_id IS NOT NULL)
                    ORDER BY v.id
                    """
                ).fetchall(),
                "orphan_chunks": conn.execute(
                    """
                    SELECT c.id, c.video_id, c.chunk_index
                    FROM chunks c LEFT JOIN videos v ON v.id = c.video_id
                    WHERE v.id IS NULL ORDER BY c.id
                    """
                ).fetchall(),
            }

            for row in checks["videos_without_chunks"]:
                self.add_issue(
                    "error",
                    "video_without_chunks",
                    f"Completed video {row['id']} has no SQLite chunks",
                    video_id=row["id"],
                    title=row["title"],
                )
            for row in checks["duplicate_sources"]:
                self.add_issue(
                    "error",
                    "duplicate_source_file_id",
                    f"source_file_id {row['source_file_id']} is used by {row['cnt']} videos",
                    source_file_id=row["source_file_id"],
                    count=row["cnt"],
                )
            for row in checks["duplicate_videos_with_chunks"]:
                self.add_issue(
                    "error",
                    "duplicate_video_has_chunks",
                    f"Duplicate video {row['id']} owns {row['chunk_count']} chunks",
                    video_id=row["id"],
                    title=row["title"],
                    original_id=row["original_id"],
                    chunk_count=row["chunk_count"],
                )
            for row in checks["duplicate_md5"]:
                self.add_issue(
                    "error",
                    "duplicate_original_md5",
                    f"MD5 {row['md5_checksum']} belongs to {row['cnt']} original videos",
                    md5_checksum=row["md5_checksum"],
                    count=row["cnt"],
                )
            for row in checks["orphan_duplicates"]:
                self.add_issue(
                    "error",
                    "orphan_duplicate",
                    f"Duplicate video {row['id']} points to missing or non-original video {row['original_id']}",
                    video_id=row["id"],
                    title=row["title"],
                    original_id=row["original_id"],
                )
            for row in checks["orphan_chunks"]:
                self.add_issue(
                    "error",
                    "orphan_sqlite_chunk",
                    f"SQLite chunk {row['id']} points to missing video {row['video_id']}",
                    chunk_id=row["id"],
                    video_id=row["video_id"],
                    chunk_index=row["chunk_index"],
                )

            low_wpm_rows = conn.execute(
                """
                SELECT v.id, v.title, v.duration_sec,
                       COALESCE(SUM(length(c.text) - length(replace(c.text, ' ', '')) + 1), 0) AS words
                FROM videos v JOIN chunks c ON c.video_id = v.id
                WHERE v.status IN ('completed', 'indexed_chunks_ready')
                  AND v.is_silent = 0 AND v.duration_sec > 30
                GROUP BY v.id HAVING (words / (v.duration_sec / 60.0)) < 10 ORDER BY v.id
                """
            ).fetchall()
            for row in low_wpm_rows:
                wpm = row["words"] / (row["duration_sec"] / 60.0)
                self.add_issue(
                    "warning",
                    "low_wpm",
                    f"Video {row['id']} has low transcript density: {wpm:.1f} WPM",
                    video_id=row["id"],
                    title=row["title"],
                    wpm=round(wpm, 2),
                )

            self.stats.update(
                {
                    "videos_without_chunks": len(checks["videos_without_chunks"]),
                    "low_wpm_videos": len(low_wpm_rows),
                    "duplicate_source_file_ids": len(checks["duplicate_sources"]),
                    "duplicate_videos_with_chunks": len(checks["duplicate_videos_with_chunks"]),
                    "duplicate_original_md5": len(checks["duplicate_md5"]),
                    "orphan_duplicates": len(checks["orphan_duplicates"]),
                    "orphan_sqlite_chunks": len(checks["orphan_chunks"]),
                }
            )

    def check_chunk_sequences(self) -> None:
        print_progress("[6/7] SQLite: chunk sequence and timestamps")
        with readonly_connection(self.sqlite_settings.db_path) as conn:
            sequence_rows = conn.execute(
                """
                WITH ordered AS (
                    SELECT id, video_id, chunk_index,
                           ROW_NUMBER() OVER (PARTITION BY video_id ORDER BY chunk_index, id) - 1 AS expected_index
                    FROM chunks
                )
                SELECT id, video_id, chunk_index, expected_index FROM ordered
                WHERE chunk_index != expected_index ORDER BY video_id, expected_index
                """
            ).fetchall()
            time_rows = conn.execute(
                """
                SELECT id, video_id, chunk_index, start_sec, end_sec FROM chunks
                WHERE start_sec IS NULL OR end_sec IS NULL OR start_sec >= end_sec OR start_sec < 0
                ORDER BY video_id, chunk_index
                """
            ).fetchall()
            duplicate_indices = conn.execute(
                """
                SELECT video_id, chunk_index, COUNT(*) AS cnt FROM chunks
                GROUP BY video_id, chunk_index HAVING COUNT(*) > 1
                ORDER BY video_id, chunk_index
                """
            ).fetchall()

            for row in sequence_rows:
                self.add_issue(
                    "error",
                    "chunk_sequence",
                    f"Chunk {row['id']} index is {row['chunk_index']}, expected {row['expected_index']}",
                    chunk_id=row["id"],
                    video_id=row["video_id"],
                    chunk_index=row["chunk_index"],
                    expected_index=row["expected_index"],
                )
            for row in time_rows:
                self.add_issue(
                    "error",
                    "chunk_time",
                    f"Chunk {row['id']} has invalid timestamps: start={row['start_sec']}, end={row['end_sec']}",
                    chunk_id=row["id"],
                    video_id=row["video_id"],
                    chunk_index=row["chunk_index"],
                    start_sec=row["start_sec"],
                    end_sec=row["end_sec"],
                )
            for row in duplicate_indices:
                self.add_issue(
                    "error",
                    "duplicate_chunk_index",
                    f"Video {row['video_id']} has {row['cnt']} chunks with index {row['chunk_index']}",
                    video_id=row["video_id"],
                    chunk_index=row["chunk_index"],
                    count=row["cnt"],
                )

            self.stats["chunk_sequence_errors"] = len(sequence_rows)
            self.stats["chunk_time_errors"] = len(time_rows)
            self.stats["duplicate_chunk_indices"] = len(duplicate_indices)

    def check_task_payloads(self) -> None:
        print_progress("[7/7] SQLite: task payload JSON")
        with readonly_connection(self.sqlite_settings.db_path) as conn:
            rows = conn.execute("SELECT id, task_type, status, payload FROM tasks ORDER BY id").fetchall()
            invalid_count = 0
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                    if not isinstance(payload, dict):
                        raise ValueError("payload is not a JSON object")
                except Exception as exc:
                    invalid_count += 1
                    self.add_issue(
                        "error",
                        "invalid_task_payload",
                        f"Task {row['id']} has invalid payload: {exc}",
                        task_id=row["id"],
                        task_type=row["task_type"],
                        status=row["status"],
                    )
            self.stats["tasks_checked"] = len(rows)
            self.stats["invalid_task_payloads"] = invalid_count

    def run(self) -> dict[str, Any]:
        self.check_sqlite_health()
        self.check_filesystem_and_sqlite()
        manticore_ids = self.check_sqlite_vs_manticore()
        self.check_vectors(manticore_ids)
        self.check_relational_logic()
        self.check_chunk_sequences()
        self.check_task_payloads()

        severity_counts = Counter(issue.severity for issue in self.issues)
        category_counts = Counter(issue.category for issue in self.issues)
        return {
            "mode": "read_only",
            "status": "clean" if not self.issues else "issues_found",
            "issue_count": len(self.issues),
            "severity_counts": dict(sorted(severity_counts.items())),
            "category_counts": dict(sorted(category_counts.items())),
            "stats": self.stats,
            "issues": [asdict(issue) for issue in self.issues],
        }

    def print_report(self, report: dict[str, Any]) -> None:
        print("\n" + "=" * 80)
        print("PULSAR READ-ONLY INTEGRITY REPORT")
        print("No files, SQLite rows, Manticore documents, or tasks were changed.")
        print("=" * 80)
        print(f"Status: {report['status']}")
        print(f"Issues: {report['issue_count']}")
        print(f"By severity: {json.dumps(report['severity_counts'], ensure_ascii=False, sort_keys=True)}")

        print("\nStatistics:")
        for key, value in sorted(report["stats"].items()):
            print(f"  {key}: {value}")

        grouped: dict[str, list[Issue]] = defaultdict(list)
        for issue in self.issues:
            grouped[issue.category].append(issue)
        if not grouped:
            print("\n✅ No integrity violations found.")
            return

        print("\nDiscrepancies:")
        for category in sorted(grouped):
            category_issues = grouped[category]
            counts = Counter(issue.severity for issue in category_issues)
            print(f"\n[{category}] total={len(category_issues)} severity={dict(sorted(counts.items()))}")
            displayed = category_issues if self.max_details == 0 else category_issues[: self.max_details]
            for issue in displayed:
                context = f" | {json.dumps(issue.context, ensure_ascii=False, sort_keys=True)}" if issue.context else ""
                print(f"  - {issue.severity.upper()}: {issue.message}{context}")
            hidden = len(category_issues) - len(displayed)
            if hidden:
                print(f"  ... {hidden} more; rerun with --max-details 0 to print all")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Pulsar integrity audit. Never repairs, deletes, queues, or updates data."
    )
    parser.add_argument(
        "--max-details",
        type=int,
        default=50,
        help="Maximum detailed issues per category; 0 prints every issue (default: 50).",
    )
    parser.add_argument(
        "--vector-sample-size",
        type=int,
        default=100,
        help="Number of deterministic Manticore vectors to validate; 0 disables vector checks (default: 100).",
    )
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    args = parser.parse_args()
    if args.max_details < 0 or args.vector_sample_size < 0:
        parser.error("limits must be zero or positive")
    return args


def main() -> int:
    args = parse_args()
    checker = ReadonlyIntegrityChecker(
        max_details=args.max_details,
        vector_sample_size=args.vector_sample_size,
    )
    report = checker.run()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        checker.print_report(report)
    return 0 if report["status"] == "clean" else 1


if __name__ == "__main__":
    raise SystemExit(main())
