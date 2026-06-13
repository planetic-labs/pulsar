#!/usr/bin/env python3
from __future__ import annotations

import gzip
import shutil
import sys
from pathlib import Path

# Fix path for standalone script execution
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_app_settings, get_manticore_settings, get_sqlite_settings
from app.db import db_connection
from app.manticore import get_manticore_client

# ANSI colors for nice terminal reporting
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"


def print_section(title: str) -> None:
    print(f"\n{BOLD}=== {title} ==={RESET}")


def print_ok(msg: str) -> None:
    print(f"{GREEN}[OK] {msg}{RESET}")


def print_warning(msg: str) -> None:
    print(f"{YELLOW}[WARNING] {msg}{RESET}")


def print_error(msg: str) -> None:
    print(f"{RED}[ERROR] {msg}{RESET}")


def run_diagnostics() -> int:
    app_settings = get_app_settings()
    sqlite_settings = get_sqlite_settings()
    manticore_settings = get_manticore_settings()

    print(f"{BOLD}Pulsar Diagnostic Tool (System & Data Integrity Check){RESET}")
    print(f"Database Path: {sqlite_settings.db_path}")
    print(f"Storage Dir: {app_settings.storage_dir}")
    print(f"Manticore URL: {manticore_settings.url}")

    errors_found = 0
    warnings_found = 0

    # ----------------------------------------------------
    # SECTION 1: SQLite Databases and Tables
    # ----------------------------------------------------
    print_section("1. SQLite Database Integrity")
    if not sqlite_settings.db_path.exists():
        print_error(f"Database file does not exist: {sqlite_settings.db_path}")
        return 1

    try:
        with db_connection(sqlite_settings) as conn:
            # Check PRAGMA integrity
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity and integrity[0].lower() == "ok":
                print_ok("PRAGMA integrity_check passed.")
            else:
                print_error(f"PRAGMA integrity_check failed: {integrity}")
                errors_found += 1

            # Count videos and statuses
            total_videos: int = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            total_chunks: int = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            print_ok(f"SQLite Stats: {total_videos} videos, {total_chunks} chunks.")

            # Status distribution
            rows = conn.execute("SELECT status, COUNT(*) FROM videos GROUP BY status").fetchall()
            status_map: dict[str, int] = {r[0]: r[1] for r in rows}
            print(f"  Status distribution: {status_map}")

            # Duplicates
            duplicates_count: int = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE original_id IS NOT NULL"
            ).fetchone()[0]
            print(f"  Duplicates linked: {duplicates_count}")

            # Check 1: Ready videos with 0 chunks (except silent ones or skipped if any)
            # A ready video must have transcripts/chunks unless there are no dialogs
            ready_no_chunks_rows = conn.execute("""
                SELECT id, title, source_file_id
                FROM videos
                WHERE status = 'ready' AND original_id IS NULL
                  AND id NOT IN (SELECT DISTINCT video_id FROM chunks)
            """).fetchall()

            if ready_no_chunks_rows:
                msg = f"Found {len(ready_no_chunks_rows)} original ready video(s) with 0 transcript chunks in SQLite:"
                print_warning(msg)
                for r in ready_no_chunks_rows[:5]:
                    print(f"    - ID {r[0]}: {r[1]} (file_id: {r[2]})")
                if len(ready_no_chunks_rows) > 5:
                    print("    - ... and more")
                warnings_found += 1
            else:
                print_ok("All original ready videos have transcript chunks.")

            # Check 2: Duplicates pointing to other duplicates (deep chains or cycles)
            invalid_duplicate_chains = conn.execute("""
                SELECT v.id, v.title, v.original_id
                FROM videos v
                JOIN videos orig ON v.original_id = orig.id
                WHERE orig.original_id IS NOT NULL
            """).fetchall()
            if invalid_duplicate_chains:
                msg_err = (
                    f"Found {len(invalid_duplicate_chains)} duplicate chains (duplicate pointing to another duplicate):"
                )
                print_error(msg_err)
                for r in invalid_duplicate_chains:
                    print(
                        f"    - Video ID {r[0]} ('{r[1]}') points to original ID {r[2]} (which is itself a duplicate)"
                    )
                errors_found += 1
            else:
                print_ok("No duplicate chains (A -> B -> C) detected in SQLite.")

    except Exception as e:
        print_error(f"Failed to query SQLite database: {e}")
        errors_found += 1

    # ----------------------------------------------------
    # SECTION 2: Transcripts Files on Disk
    # ----------------------------------------------------
    print_section("2. Sharded Transcript Files on Disk")
    try:
        with db_connection(sqlite_settings) as conn:
            # We only check original ready videos (duplicates refer to original transcripts)
            ready_videos = conn.execute("""
                SELECT id, source_file_id, title
                FROM videos
                WHERE status = 'ready' AND original_id IS NULL
            """).fetchall()

        missing_raw = 0
        missing_norm = 0
        corrupted_files = 0

        for r_vid, file_id, _title in ready_videos:
            raw_path = app_settings.get_raw_transcript_path(file_id)
            norm_path = app_settings.get_normalized_transcript_path(file_id)

            # Check raw transcript
            if not raw_path.exists():
                missing_raw += 1
                if missing_raw <= 5:
                    print_warning(f"Missing raw transcript for ID {r_vid}: {raw_path.name}")
            else:
                # Try opening to check corruption
                try:
                    with gzip.open(raw_path, "rt", encoding="utf-8") as f:
                        f.read(10)
                except Exception:
                    corrupted_files += 1
                    print_error(f"Corrupted gzip raw transcript for ID {r_vid}: {raw_path}")

            # Check normalized transcript
            if not norm_path.exists():
                missing_norm += 1
                if missing_norm <= 5:
                    print_warning(f"Missing normalized transcript for ID {r_vid}: {norm_path.name}")
            else:
                # Try opening
                try:
                    with gzip.open(norm_path, "rt", encoding="utf-8") as f:
                        f.read(10)
                except Exception:
                    corrupted_files += 1
                    print_error(f"Corrupted gzip normalized transcript for ID {r_vid}: {norm_path}")

        if missing_raw == 0 and missing_norm == 0 and corrupted_files == 0:
            print_ok(f"All {len(ready_videos)} transcript file pairs exist on disk and are readable.")
        else:
            if missing_raw:
                print_error(f"Total missing raw transcripts (.json.gz): {missing_raw}")
                errors_found += 1
            if missing_norm:
                print_error(f"Total missing normalized transcripts (.json.gz): {missing_norm}")
                errors_found += 1
            if corrupted_files:
                print_error(f"Total corrupted gzip transcript files: {corrupted_files}")
                errors_found += 1

    except Exception as e:
        print_error(f"Error checking transcript files: {e}")
        errors_found += 1

    # ----------------------------------------------------
    # SECTION 3: Manticore Search Index Sync
    # ----------------------------------------------------
    print_section("3. Manticore Search Index Sync")
    try:
        client = get_manticore_client()
        # Ping and get status
        res = client._execute_sql("SHOW TABLES")
        tables = []
        if res and len(res) > 0:
            data = res[0].get("data", [])
            for r in data:
                if isinstance(r, list):
                    tables.append(r[0])
                elif isinstance(r, dict):
                    table_name = r.get("Table") or r.get("Index") or r.get("index")
                    if table_name:
                        tables.append(table_name)

        if manticore_settings.table_name not in tables:
            m_tbl = manticore_settings.table_name
            print_error(f"Table '{m_tbl}' does not exist in Manticore. Existing: {tables}")
            errors_found += 1
        else:
            print_ok(f"Connected to Manticore. Index '{manticore_settings.table_name}' exists.")

            # Get document count in Manticore
            meta_res = client._execute_sql(f"SELECT COUNT(*) FROM {manticore_settings.table_name}")
            manticore_chunks = 0
            if meta_res and len(meta_res) > 0:
                data_rows = meta_res[0].get("data", [])
                if data_rows:
                    row = data_rows[0]
                    if isinstance(row, list) and len(row) > 0:
                        manticore_chunks = row[0]
                    elif isinstance(row, dict) and len(row) > 0:
                        manticore_chunks = list(row.values())[0]

            print(f"  SQLite chunks: {total_chunks}")
            print(f"  Manticore chunks: {manticore_chunks}")

            if total_chunks == manticore_chunks:
                print_ok("SQLite and Manticore chunk counts match perfectly.")
            else:
                diff = abs(total_chunks - manticore_chunks)
                msg_cnt = (
                    f"Chunk count mismatch: Diff of {diff} chunk(s) between "
                    f"SQLite ({total_chunks}) and Manticore ({manticore_chunks})."
                )
                print_warning(msg_cnt)
                warnings_found += 1

            # Check for orphaned chunks in Manticore
            # Retrieve a sample of video IDs from Manticore and verify they exist in SQLite
            sql_query = f"SELECT video_id FROM {manticore_settings.table_name} GROUP BY video_id LIMIT 100"
            sample_res = client._execute_sql(sql_query)
            if sample_res and len(sample_res) > 0:
                m_video_ids = [
                    r[0] if isinstance(r, list) else r.get("video_id") for r in sample_res[0].get("data", [])
                ]

                with db_connection(sqlite_settings) as conn:
                    placeholders = ",".join("?" for _ in m_video_ids)
                    valid_ids = {
                        str(row[0])
                        for row in conn.execute(
                            f"SELECT id FROM videos WHERE id IN ({placeholders})", m_video_ids
                        ).fetchall()
                    }

                orphans = [vid for vid in m_video_ids if str(vid) not in valid_ids]
                if orphans:
                    msg_orph = (
                        f"Found orphaned video index references in Manticore "
                        f"(IDs do not exist in SQLite): {orphans[:5]}"
                    )
                    print_error(msg_orph)
                    errors_found += 1
                else:
                    print_ok("Manticore index integrity check passed (sampled references exist).")

    except Exception as e:
        print_error(f"Failed to communicate with Manticore: {e}")
        errors_found += 1

    # ----------------------------------------------------
    # SECTION 4: System Resources & Buffer Space
    # ----------------------------------------------------
    print_section("4. Disk Space & System Resources")
    try:
        total, used, free = shutil.disk_usage(app_settings.storage_dir)
        free_gb = free / (1024**3)
        buffer_gb = app_settings.disk_space_buffer_gb
        print(f"  Storage volume: {total / (1024**3):.1f} GB total, {free_gb:.1f} GB free.")

        if free_gb < buffer_gb:
            msg_space = (
                f"Free disk space ({free_gb:.1f} GB) is below "
                f"safety buffer size ({buffer_gb} GB). Ingestion might pause."
            )
            print_warning(msg_space)
            warnings_found += 1
        else:
            print_ok(f"Disk space is healthy. Safety buffer ({buffer_gb} GB) is satisfied.")

    except Exception as e:
        print_error(f"Could not read disk space of {app_settings.storage_dir}: {e}")
        warnings_found += 1

    # ----------------------------------------------------
    # DIAGNOSTIC SUMMARY
    # ----------------------------------------------------
    print_section("Summary Report")
    print(f"  Errors:   {errors_found}")
    print(f"  Warnings: {warnings_found}")

    if errors_found > 0:
        print_error(f"Diagnostics finished with {errors_found} errors. Action is required.")
        return 2
    elif warnings_found > 0:
        msg_warn = f"Diagnostics finished with {warnings_found} warnings. System is operational but check the issues."
        print_warning(msg_warn)
        return 0
    else:
        print_ok("All diagnostic tests passed successfully! System is perfectly healthy.")
        return 0


if __name__ == "__main__":
    sys.exit(run_diagnostics())
