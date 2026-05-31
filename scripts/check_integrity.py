import json
import logging
import random
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_app_settings, get_qdrant_settings, get_sqlite_settings
from app.db import db_connection
from app.qdrant import get_qdrant_client

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("integrity_check")


def check_integrity() -> list[str]:
    app_settings = get_app_settings()
    sqlite_settings = get_sqlite_settings()
    q_settings = get_qdrant_settings()

    qdrant = get_qdrant_client()

    print("\n=== [1] ПРОВЕРКА ФАЙЛОВОЙ СИСТЕМЫ И SQLITE ===")

    issues = []
    db_raw_files = set()
    db_norm_files = set()
    corrupted_json = []
    missing_files = []
    text_mismatch_errors = []

    with db_connection(sqlite_settings) as conn:
        sql = "SELECT id, video_id, raw_json_path, normalized_json_path FROM transcripts"
        transcripts = conn.execute(sql).fetchall()

        for t in transcripts:
            # Check RAW
            if t["raw_json_path"]:
                raw_path = Path(t["raw_json_path"])
                db_raw_files.add(raw_path.resolve())
                if not raw_path.exists():
                    msg = f"Video {t['video_id']}: Missing RAW at {raw_path}"
                    missing_files.append(msg)
                    issues.append(msg)
                else:
                    try:
                        with open(raw_path, encoding="utf-8") as f:
                            json.load(f)
                    except Exception:
                        msg = f"Video {t['video_id']}: Corrupted RAW JSON at {raw_path}"
                        corrupted_json.append(msg)
                        issues.append(msg)

            # Check NORMALIZED + text comparison
            if t["normalized_json_path"]:
                norm_path = Path(t["normalized_json_path"])
                db_norm_files.add(norm_path.resolve())
                if not norm_path.exists():
                    msg = f"Video {t['video_id']}: Missing NORMALIZED at {norm_path}"
                    missing_files.append(msg)
                    issues.append(msg)
                else:
                    try:
                        with open(norm_path, encoding="utf-8") as f:
                            norm_data = json.load(f)

                        # Check text of the first chunk
                        sql_chunk = "SELECT text FROM chunks WHERE transcript_id = ? ORDER BY chunk_index LIMIT 1"
                        first_chunk = conn.execute(sql_chunk, (t["id"],)).fetchone()

                        if first_chunk and "utterances" in norm_data and len(norm_data["utterances"]) > 0:
                            json_text = norm_data["utterances"][0].get("text", "")
                            if json_text and not first_chunk["text"].startswith(json_text):
                                if first_chunk["text"].strip().startswith(json_text.strip()):
                                    continue
                                msg = f"Video {t['video_id']}: Text mismatch between DB and JSON"
                                text_mismatch_errors.append(msg)
                                issues.append(msg)
                    except Exception as e:
                        msg = f"Video {t['video_id']}: Corrupted NORM JSON at {norm_path} ({e})"
                        corrupted_json.append(msg)
                        issues.append(msg)

    print(f"Записей в базе: {len(transcripts)}")
    if missing_files:
        print(f"❌ Пропущено файлов: {len(missing_files)}")
        for m in missing_files[:10]:
            print(f"  - {m}")
    else:
        print("✅ Все файлы из базы на месте.")

    if corrupted_json:
        print(f"❌ Битых JSON файлов: {len(corrupted_json)}")
        for c in corrupted_json[:10]:
            print(f"  - {c}")
    else:
        print("✅ Все JSON файлы валидны.")

    if text_mismatch_errors:
        print(f"⚠️  Рассинхрон текста (БД vs JSON): {len(text_mismatch_errors)}")
        for tm in text_mismatch_errors[:5]:
            print(f"  - {tm}")
    else:
        print("✅ Текст в БД соответствует файлам на диске.")

    # Search orphan files
    print("\n--- Поиск файлов-сирот (есть на диске, нет в базе) ---")
    all_raw_on_disk = {p.resolve() for p in app_settings.raw_transcripts_dir.glob("**/*.json")}
    all_norm_on_disk = {p.resolve() for p in app_settings.normalized_transcripts_dir.glob("*.json")}

    orphan_raw = all_raw_on_disk - db_raw_files
    orphan_norm = all_norm_on_disk - db_norm_files

    if orphan_raw:
        print(f"⚠️  Сиротских RAW файлов: {len(orphan_raw)}")
        issues.append(f"Orphan RAW files on disk: {len(orphan_raw)}")
    if orphan_norm:
        print(f"⚠️  Сиротских NORMALIZED файлов: {len(orphan_norm)}")
        issues.append(f"Orphan NORMALIZED files on disk: {len(orphan_norm)}")
    if not orphan_raw and not orphan_norm:
        print("✅ Лишних файлов не обнаружено.")

    print("\n=== [2] ПРОВЕРКА SQLITE И QDRANT ===")

    with db_connection(sqlite_settings) as conn:
        sql_chunks = conn.execute("SELECT id, text FROM chunks").fetchall()
        db_chunk_map = {r["id"]: r["text"] for r in sql_chunks}
        db_chunk_ids = set(db_chunk_map.keys())

        sql_v = "SELECT id, title FROM videos WHERE processing_status IN ('completed', 'indexed_chunks_ready')"
        completed_videos = conn.execute(sql_v).fetchall()

    print(f"Чанков в SQLite: {len(db_chunk_ids)}")

    print("Получение данных из Qdrant...")
    q_point_ids = set()
    q_sample_points = []

    offset = None
    while True:
        points, next_offset = qdrant.scroll(
            collection_name=q_settings.collection_name,
            limit=10000,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for p in points:
            q_point_ids.add(p.id)
            if len(q_sample_points) < 100:
                q_sample_points.append(p)

        if not next_offset:
            break
        offset = next_offset

    print(f"Точек в Qdrant: {len(q_point_ids)}")

    missing_in_qdrant = db_chunk_ids - q_point_ids
    orphan_in_qdrant = q_point_ids - db_chunk_ids

    if missing_in_qdrant:
        msg = f"Chunks in SQLite missing in Qdrant: {len(missing_in_qdrant)}"
        print(f"❌ {msg}")
        issues.append(msg)
    else:
        print("✅ Все чанки из базы есть в поиске.")

    if orphan_in_qdrant:
        msg = f"Orphan points in Qdrant (missing in SQLite): {len(orphan_in_qdrant)}"
        print(f"⚠️  {msg}")
        issues.append(msg)
    else:
        print("✅ В Qdrant нет лишних данных.")

    # Check sample Qdrant metadata vs SQLite
    metadata_errors = 0
    print("--- Выборочная проверка метаданных (Qdrant vs SQLite) ---")
    for p in random.sample(q_sample_points, min(len(q_sample_points), 20)):
        db_text = db_chunk_map.get(p.id)
        q_text = p.payload.get("text")
        if db_text and q_text and db_text.strip() != q_text.strip():
            print(f"❌ Payload mismatch for point {p.id}!")
            metadata_errors += 1
            issues.append(f"Payload mismatch for point {p.id} in Qdrant vs SQLite")

    if metadata_errors == 0:
        print("✅ Выборочная проверка метаданных пройдена.")

    print("\n=== [3] ЛОГИЧЕСКИЕ ОШИБКИ И ЭВРИСТИКА ===")

    with db_connection(sqlite_settings) as conn:
        # 1. Videos without chunks
        for v in completed_videos:
            count = conn.execute("SELECT COUNT(*) as cnt FROM chunks WHERE video_id = ?", (v["id"],)).fetchone()["cnt"]
            if count == 0:
                msg = f"Video '{v['title']}' (ID:{v['id']}): 0 chunks in DB!"
                print(f"❌ {msg}")
                issues.append(msg)

            # 2. Heuristics: wpm density
            video_data = conn.execute("SELECT duration_sec FROM videos WHERE id = ?", (v["id"],)).fetchone()
            if video_data and video_data["duration_sec"] and video_data["duration_sec"] > 30:
                duration_min = video_data["duration_sec"] / 60
                sql_words = """
                    SELECT SUM((length(text) - length(replace(text, ' ', '')) + 1)) as w_cnt
                    FROM chunks WHERE video_id = ?
                """
                words_count = conn.execute(sql_words, (v["id"],)).fetchone()["w_cnt"] or 0

                wpm = words_count / duration_min
                if wpm < 10:
                    msg = f"Low WPM density for video '{v['title']}' (ID:{v['id']}): {wpm:.1f} WPM"
                    print(f"⚠️  {msg}")
                    issues.append(msg)

        # 3. Duplicate source_file_id
        sql_dupes = """
            SELECT source_file_id, COUNT(*) as cnt FROM videos
            GROUP BY source_file_id HAVING cnt > 1
        """
        dupes = conn.execute(sql_dupes).fetchall()
        if dupes:
            msg = f"Duplicate source_file_ids in videos table: {len(dupes)}"
            print(f"❌ {msg}")
            issues.append(msg)
            for d in dupes:
                print(f"  - {d['source_file_id']} ({d['cnt']} раз)")
        else:
            print("✅ Дубликатов видео не обнаружено.")

    print("\n=== [4] ГЛУБОКАЯ ПРОВЕРКА ЦЕЛОСТНОСТИ CHUNKS ===")

    with db_connection(sqlite_settings) as conn:
        sql_stats = """
            SELECT video_id, COUNT(*) as total, MIN(chunk_index) as min_idx, MAX(chunk_index) as max_idx
            FROM chunks GROUP BY video_id
        """
        video_stats = conn.execute(sql_stats).fetchall()

        sequence_errors = 0
        time_logic_errors = 0

        for vs in video_stats:
            v_id = vs["video_id"]
            sql_c = "SELECT chunk_index, start_sec, end_sec FROM chunks WHERE video_id = ? ORDER BY chunk_index"
            chunks = conn.execute(sql_c, (v_id,)).fetchall()

            for i, c in enumerate(chunks):
                if c["chunk_index"] != i:
                    sequence_errors += 1
                if c["start_sec"] >= c["end_sec"]:
                    time_logic_errors += 1

        if sequence_errors:
            msg = f"Chunk sequence errors in DB: {sequence_errors}"
            print(f"❌ {msg}")
            issues.append(msg)
        else:
            print("✅ Индексы чанков последовательны.")

        if time_logic_errors:
            msg = f"Chunk time logic errors (start >= end): {time_logic_errors}"
            print(f"❌ {msg}")
            issues.append(msg)
        else:
            print("✅ Таймкоды чанков логически верны.")

    print("\n=== ПРОВЕРКА ЗАВЕРШЕНА ===")
    return issues


if __name__ == "__main__":
    check_integrity()
