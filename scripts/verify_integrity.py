import gzip
import json
import logging
import random
import sys
from pathlib import Path
from typing import Any

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.chunking import chunk_from_utterances
from app.config import get_app_settings, get_embedding_settings, get_manticore_settings, get_sqlite_settings
from app.db import db_connection
from app.indexing_state import enqueue_index_task
from app.integrity_reporter import format_integrity_issues_for_telegram
from app.manticore import get_manticore_client
from app.repository import replace_chunks

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("integrity_check")


class IntegrityChecker:
    """Координатор проверок целостности системы (ФС + SQLite + Manticore)."""

    def __init__(self) -> None:
        self.app_settings = get_app_settings()
        self.sqlite_settings = get_sqlite_settings()
        self.q_settings = get_manticore_settings()
        self.manticore = get_manticore_client()
        self.issues: list[str] = []

        # Счётчики изменений
        self.deleted_raw_count = 0
        self.deleted_norm_count = 0
        self.reindexed_videos_count = 0
        self.reindexed_chunks_count = 0
        self.deleted_manticore_points_count = 0

    def check_worker_status(self) -> dict[str, Any] | None:
        """Проверяет активность воркера."""
        with db_connection(self.sqlite_settings) as conn:
            active_tasks = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE status IN ('pending', 'running')"
            ).fetchone()
            active_tasks_count = active_tasks["cnt"] if active_tasks else 0

        if active_tasks_count > 0:
            print(f"\n⚠️  Воркер занят. В очереди задач: {active_tasks_count}.")
            print("Проверка целостности отложена во избежание конфликтов данных.")
            return {
                "status": "worker_running",
                "active_tasks_count": active_tasks_count,
                "issues": [],
                "deleted_raw_count": 0,
                "deleted_norm_count": 0,
                "reindexed_videos_count": 0,
                "reindexed_chunks_count": 0,
                "deleted_manticore_points_count": 0,
            }
        return None

    def check_filesystem_and_sqlite(self) -> tuple[set[Path], set[Path]]:
        """Выполняет проверку файловой системы и сопоставления с базой данных SQLite."""
        print("\n=== [1] ПРОВЕРКА ФАЙЛОВОЙ СИСТЕМЫ И SQLITE ===")
        db_raw_files: set[Path] = set()
        db_norm_files: set[Path] = set()
        corrupted_json: list[str] = []
        missing_files: list[str] = []
        text_mismatch_errors: list[str] = []

        with db_connection(self.sqlite_settings) as conn:
            sql = """
                SELECT id as video_id, source_file_id, title, is_short
                FROM videos
                WHERE original_id IS NULL AND is_silent = 0
            """
            videos = conn.execute(sql).fetchall()

            for v in videos:
                file_id = v["source_file_id"]
                if not file_id:
                    continue

                raw_path = self.app_settings.get_raw_transcript_path(file_id)
                norm_path = self.app_settings.get_normalized_transcript_path(file_id)

                # Check RAW
                db_raw_files.add(raw_path.resolve())
                if not raw_path.exists():
                    msg = f"Video {v['video_id']}: Missing RAW at {raw_path}"
                    missing_files.append(msg)
                    self.issues.append(msg)
                else:
                    try:
                        with gzip.open(raw_path, "rt", encoding="utf-8") as f:
                            json.load(f)
                    except Exception:
                        msg = f"Video {v['video_id']}: Corrupted RAW JSON at {raw_path}"
                        corrupted_json.append(msg)
                        self.issues.append(msg)

                # Check NORMALIZED + text comparison
                db_norm_files.add(norm_path.resolve())
                if not norm_path.exists():
                    msg = f"Video {v['video_id']}: Missing NORMALIZED at {norm_path}"
                    missing_files.append(msg)
                    self.issues.append(msg)
                else:
                    try:
                        with gzip.open(norm_path, "rt", encoding="utf-8") as f:
                            norm_data = json.load(f)

                        # Check text of the first chunk
                        sql_chunk = "SELECT text FROM chunks WHERE video_id = ? ORDER BY chunk_index LIMIT 1"
                        first_chunk = conn.execute(sql_chunk, (v["video_id"],)).fetchone()

                        if first_chunk and "utterances" in norm_data and len(norm_data["utterances"]) > 0:
                            json_text = norm_data["utterances"][0].get("text", "")
                            if (
                                json_text
                                and not first_chunk["text"].startswith(json_text)
                                and not first_chunk["text"].strip().startswith(json_text.strip())
                            ):
                                msg = f"Video {v['video_id']}: Text mismatch between DB and JSON"
                                text_mismatch_errors.append(msg)
                                self.issues.append(msg)

                        # Check chunk count matching
                        is_short_val = bool(v["is_short"])
                        raw_chunks = norm_data.get("utterances") or norm_data.get("chunks") or []
                        expected_chunks = chunk_from_utterances(raw_chunks, single_chunk=is_short_val)
                        expected_count = len(expected_chunks)

                        actual_count = conn.execute(
                            "SELECT COUNT(*) as cnt FROM chunks WHERE video_id = ?", (v["video_id"],)
                        ).fetchone()["cnt"]

                        if actual_count != expected_count:
                            msg = (
                                f"Video '{v['title']}' (ID:{v['video_id']}): "
                                f"chunk count mismatch. DB has {actual_count}, expected {expected_count} "
                                f"(is_short={is_short_val})."
                            )
                            self.issues.append(msg)
                            print(f"⚠️  {msg} Восстанавливаем чанки...")

                            # Auto-heal
                            replace_chunks(conn, video_id=v["video_id"], chunks=expected_chunks)

                            enqueue_index_task(conn, video_id=v["video_id"], title=v["title"], priority=5)
                            self.reindexed_videos_count += 1
                            print(f"✅ Пересоздано чанков: {expected_count}, видео поставлено в очередь на индексацию.")
                    except Exception as e:
                        msg = f"Video {v['video_id']}: Corrupted NORM JSON at {norm_path} ({e})"
                        corrupted_json.append(msg)
                        self.issues.append(msg)

        print(f"Оригинальных видео в базе: {len(videos)}")
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

        # Clean up orphan files
        print("\n--- Поиск и очистка файлов-сирот (есть на диске, нет в базе) ---")
        all_raw_on_disk = {p.resolve() for p in self.app_settings.raw_transcripts_dir.glob("**/*.json.gz")}
        all_norm_on_disk = {p.resolve() for p in self.app_settings.normalized_transcripts_dir.glob("**/*.json.gz")}

        orphan_raw = all_raw_on_disk - db_raw_files
        orphan_norm = all_norm_on_disk - db_norm_files

        if orphan_raw:
            print(f"⚠️  Найдено сиротских RAW файлов: {len(orphan_raw)}. Удаление...")
            for p in orphan_raw:
                try:
                    p.unlink()
                    self.deleted_raw_count += 1
                except Exception as e:
                    msg = f"Failed to delete orphan RAW file {p}: {e}"
                    print(f"❌ {msg}")
                    self.issues.append(msg)

        if orphan_norm:
            print(f"⚠️  Найдено сиротских NORMALIZED файлов: {len(orphan_norm)}. Удаление...")
            for p in orphan_norm:
                try:
                    p.unlink()
                    self.deleted_norm_count += 1
                except Exception as e:
                    msg = f"Failed to delete orphan NORMALIZED file {p}: {e}"
                    print(f"❌ {msg}")
                    self.issues.append(msg)

        if self.deleted_raw_count:
            print(f"✅ Успешно удалено сиротских RAW файлов: {self.deleted_raw_count}")
        if self.deleted_norm_count:
            print(f"✅ Успешно удалено сиротских NORMALIZED файлов: {self.deleted_norm_count}")

        if not orphan_raw and not orphan_norm:
            print("✅ Лишних файлов не обнаружено.")

        return db_raw_files, db_norm_files

    def check_sqlite_vs_manticore(self) -> tuple[set[int], dict[int, dict[str, Any]]]:
        """Выполняет сопоставление чанков между SQLite и Manticore."""
        print("\n=== [2] ПРОВЕРКА SQLITE И MANTICORE ===")
        with db_connection(self.sqlite_settings) as conn:
            sql_chunks = conn.execute("SELECT id, text FROM chunks").fetchall()
            db_chunk_map = {r["id"]: r["text"] for r in sql_chunks}
            db_chunk_ids = set(db_chunk_map.keys())

        print(f"Чанков в SQLite: {len(db_chunk_ids)}")
        print("Получение данных из Manticore (постранично)...")

        q_point_ids = set()
        q_point_map: dict[int, dict[str, Any]] = {}

        last_id = 0
        batch_size = 5000
        while True:
            sql = (
                f"SELECT id, video_id, chunk_index, text FROM {self.q_settings.table_name} "
                f"WHERE id > {last_id} ORDER BY id ASC LIMIT {batch_size} "
                f"OPTION max_matches={batch_size}"
            )
            try:
                res = self.manticore._execute_sql(sql)
            except Exception as e:
                print(f"❌ Failed to query Manticore: {e}")
                self.issues.append(f"Manticore query failed: {e}")
                break

            if not res or len(res) == 0:
                break
            data = res[0].get("data", [])
            if not data:
                break
            columns = [next(iter(c.keys())) for c in res[0].get("columns", [])]
            for row in data:
                r_dict = row if isinstance(row, dict) else dict(zip(columns, row, strict=False))
                doc_id = r_dict["id"]
                last_id = max(last_id, doc_id)
                q_point_ids.add(doc_id)
                q_point_map[doc_id] = {
                    "text": r_dict.get("text") or "",
                    "video_id": r_dict.get("video_id"),
                    "chunk_index": r_dict.get("chunk_index"),
                }
            if len(data) < batch_size:
                break

        print(f"Точек в Manticore: {len(q_point_ids)}")

        missing_in_manticore = db_chunk_ids - q_point_ids
        orphan_in_manticore = q_point_ids - db_chunk_ids

        if missing_in_manticore:
            msg = f"Chunks in SQLite missing in Manticore: {len(missing_in_manticore)}"
            print(f"⚠️  {msg}. Инициируем автоматическую повторную индексацию через воркер...")

            missing_list = list(missing_in_manticore)
            batch_size_sel = 500
            video_map = {}

            with db_connection(self.sqlite_settings) as conn:
                for i in range(0, len(missing_list), batch_size_sel):
                    batch = missing_list[i : i + batch_size_sel]
                    placeholders = ",".join(["?"] * len(batch))
                    sql_sel = f"""
                        SELECT DISTINCT v.id, v.title
                        FROM chunks c
                        JOIN videos v ON v.id = c.video_id
                        WHERE c.id IN ({placeholders})
                    """
                    rows = conn.execute(sql_sel, batch).fetchall()
                    for r in rows:
                        video_map[r["id"]] = r["title"]

                # Read active stage_3_index tasks
                active_tasks = conn.execute(
                    "SELECT payload FROM tasks WHERE task_type = 'stage_3_index' AND status IN ('pending', 'running')"
                ).fetchall()
                active_video_ids = set()
                for t in active_tasks:
                    try:
                        p = json.loads(t["payload"])
                        if "video_id" in p:
                            active_video_ids.add(p["video_id"])
                    except Exception:
                        pass

                # Queue missing for indexing
                for video_id, title in video_map.items():
                    if video_id in active_video_ids:
                        print(f"  - Видео '{title}' (ID:{video_id}) уже в очереди на индексацию. Пропускаем.")
                        continue

                    try:
                        enqueue_index_task(conn, video_id=video_id, title=title, priority=5)
                        self.reindexed_videos_count += 1
                        print(f"  - Добавлена задача индексации для видео '{title}' (ID:{video_id})")
                    except Exception as e:
                        err_msg = f"Failed to queue indexing task for video ID {video_id}: {e}"
                        print(f"❌ {err_msg}")
                        self.issues.append(err_msg)

            print(f"✅ Отправлено задач на переиндексацию: {self.reindexed_videos_count}")
            self.reindexed_chunks_count = len(missing_in_manticore)
        else:
            print("✅ Все чанки из базы есть в поиске.")

        if orphan_in_manticore:
            msg = f"Orphan points in Manticore (missing in SQLite): {len(orphan_in_manticore)}"
            print(f"⚠️  {msg}. Удаляем сиротские точки из Manticore...")
            try:
                orphan_list = list(orphan_in_manticore)
                for k in range(0, len(orphan_list), 1000):
                    sub_list = orphan_list[k : k + 1000]
                    self.manticore.delete(
                        collection_name=self.q_settings.table_name,
                        ids=sub_list,
                    )
                self.deleted_manticore_points_count += len(orphan_list)
                print(f"✅ Успешно удалено точек из Manticore: {self.deleted_manticore_points_count}")
            except Exception as e:
                err_msg = f"Failed to delete orphan points from Manticore: {e}"
                print(f"❌ {err_msg}")
                self.issues.append(err_msg)
        else:
            print("✅ В Manticore нет лишних данных.")

        # Check all Manticore metadata vs SQLite
        metadata_errors = 0
        print("--- Полная проверка метаданных (Manticore vs SQLite) ---")
        for s_id in db_chunk_ids:
            db_text = db_chunk_map.get(s_id)
            m_chunk = q_point_map.get(s_id)
            if not m_chunk:
                print(f"❌ Чанк SQLite ID {s_id} ОТСУТСТВУЕТ в Manticore!")
                metadata_errors += 1
                self.issues.append(f"Chunk SQLite ID {s_id} is missing in Manticore")
                continue
            q_text = m_chunk["text"]
            if db_text and q_text and db_text.strip() != q_text.strip():
                if metadata_errors < 10:
                    print(f"❌ Несоответствие текста для чанка {s_id}!")
                    print(f"  SQLite: {db_text.strip()[:100]}...")
                    print(f"  Manticore: {q_text.strip()[:100]}...")
                metadata_errors += 1
                self.issues.append(f"Text mismatch for chunk ID {s_id} in Manticore vs SQLite")

        if metadata_errors == 0:
            print(f"✅ Полная проверка метаданных пройдена ({len(db_chunk_ids)} чанков проверено).")
        else:
            print(f"❌ Полная проверка метаданных выявила {metadata_errors} ошибок!")

        return db_chunk_ids, q_point_map

    def check_vector_data(self, q_point_ids: set[int]) -> None:
        """Проверяет векторные данные в Manticore."""
        print("\n=== [2.5] ПРОВЕРКА ВЕКТОРНЫХ ДАННЫХ ===")
        emb_settings = get_embedding_settings()
        expected_dim = emb_settings.dimension or 1024
        print(f"Ожидаемая размерность векторов: {expected_dim}")

        if q_point_ids:
            sample_ids = random.sample(list(q_point_ids), min(100, len(q_point_ids)))
            ids_str = ",".join(str(x) for x in sample_ids)
            sql_vecs = f"SELECT id, vec FROM {self.q_settings.table_name} WHERE id IN ({ids_str})"

            try:
                res_vecs = self.manticore._execute_sql(sql_vecs)
                vec_errors = 0
                checked_vecs = 0

                if res_vecs and len(res_vecs) > 0:
                    cols_meta = res_vecs[0].get("columns")
                    if isinstance(cols_meta, list):
                        columns = [next(iter(c.keys())) for c in cols_meta]
                    else:
                        columns = list(cols_meta.keys()) if cols_meta else []
                    data = res_vecs[0].get("data", [])

                    for row in data:
                        row_dict = row if isinstance(row, dict) else dict(zip(columns, row, strict=False))

                        doc_id = row_dict["id"]
                        vec_val = row_dict.get("vec")

                        if not vec_val:
                            msg = f"Vector check: Chunk ID {doc_id} has empty or missing vector!"
                            print(f"❌ {msg}")
                            self.issues.append(msg)
                            vec_errors += 1
                            continue

                        if isinstance(vec_val, str):
                            parts = vec_val.split(",")
                            try:
                                vec_floats = [float(x) for x in parts if x.strip()]
                            except ValueError:
                                vec_floats = []
                        elif isinstance(vec_val, list):
                            vec_floats = vec_val
                        else:
                            vec_floats = []

                        checked_vecs += 1
                        actual_dim = len(vec_floats)

                        if actual_dim != expected_dim:
                            msg = (
                                f"Vector check: Chunk ID {doc_id} dim mismatch: "
                                f"expected {expected_dim}, got {actual_dim}"
                            )
                            print(f"❌ {msg}")
                            self.issues.append(msg)
                            vec_errors += 1
                            continue

                        sq_sum = sum(x * x for x in vec_floats)
                        if sq_sum < 1e-6:
                            msg = f"Vector check: Chunk ID {doc_id} zero vector (norm < 1e-6)"
                            print(f"❌ {msg}")
                            self.issues.append(msg)
                            vec_errors += 1

                if vec_errors == 0:
                    print(f"✅ Векторная проверка пройдена ({checked_vecs} случайных векторов проверено).")
                else:
                    print(f"❌ Векторная проверка выявила {vec_errors} ошибок!")
            except Exception as e:
                msg = f"Failed to execute vector validation query: {e}"
                print(f"❌ {msg}")
                self.issues.append(msg)

    def check_logical_and_heuristics(self) -> None:
        """Проверяет логику связей и эвристики (дубликаты, WPM, сироты)."""
        print("\n=== [3] ЛОГИЧЕСКИЕ ОШИБКИ И ЭВРИСТИКА ===")
        with db_connection(self.sqlite_settings) as conn:
            sql_v = """
                SELECT id, title
                FROM videos
                WHERE status IN ('completed', 'indexed_chunks_ready') AND is_silent = 0
            """
            completed_videos = conn.execute(sql_v).fetchall()

            # 1. Videos without chunks
            for v in completed_videos:
                count = conn.execute("SELECT COUNT(*) as cnt FROM chunks WHERE video_id = ?", (v["id"],)).fetchone()[
                    "cnt"
                ]
                if count == 0:
                    msg = f"Video '{v['title']}' (ID:{v['id']}): 0 chunks in DB!"
                    print(f"❌ {msg}")
                    self.issues.append(msg)

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
                        self.issues.append(msg)

            # 3. Duplicate source_file_id
            sql_dupes = """
                SELECT source_file_id, COUNT(*) as cnt FROM videos
                GROUP BY source_file_id HAVING cnt > 1
            """
            dupes = conn.execute(sql_dupes).fetchall()
            if dupes:
                msg = f"Duplicate source_file_ids in videos table: {len(dupes)}"
                print(f"❌ {msg}")
                self.issues.append(msg)
                for d in dupes:
                    print(f"  - {d['source_file_id']} ({d['cnt']} раз)")
            else:
                print("✅ Дубликатов видео не обнаружено.")

            # 4. Check that duplicate videos have no chunks
            sql_dup_issues = """
                SELECT id, title FROM videos
                WHERE original_id IS NOT NULL AND (
                    id IN (SELECT DISTINCT video_id FROM chunks)
                )
            """
            dup_issues = conn.execute(sql_dup_issues).fetchall()
            if dup_issues:
                for di in dup_issues:
                    msg = f"Duplicate video '{di['title']}' (ID:{di['id']}) has chunks in DB!"
                    print(f"❌ {msg} Удаляем лишние чанки...")
                    self.issues.append(msg)

                    chunk_rows = conn.execute("SELECT id FROM chunks WHERE video_id = ?", (di["id"],)).fetchall()
                    chunk_ids = [r["id"] for r in chunk_rows]
                    if chunk_ids:
                        try:
                            self.manticore.delete(collection_name=self.q_settings.table_name, ids=chunk_ids)
                            print(f"  - Удалено {len(chunk_ids)} векторов из Manticore")
                        except Exception as me:
                            print(f"  - Ошибка удаления из Manticore: {me}")
                        conn.execute("DELETE FROM chunks WHERE video_id = ?", (di["id"],))
                        print(f"  - Удалены чанки из SQLite для видео ID {di['id']}")
            else:
                print("✅ Дубликаты не содержат лишних чанков.")

            # 5. Check for multiple originals with the same MD5
            sql_md5_dupes = """
                SELECT md5_checksum, COUNT(*) as cnt FROM videos
                WHERE original_id IS NULL AND md5_checksum IS NOT NULL AND md5_checksum != ''
                GROUP BY md5_checksum HAVING cnt > 1
            """
            md5_dupes = conn.execute(sql_md5_dupes).fetchall()
            if md5_dupes:
                for md in md5_dupes:
                    md5_val = md["md5_checksum"]
                    msg = f"Multiple original videos share the same MD5 checksum '{md5_val}': {md['cnt']} files"
                    print(f"❌ {msg} Оставляем самое старое оригиналом, остальные переводим в дубликаты...")
                    self.issues.append(msg)

                    orig_rows = conn.execute(
                        "SELECT id, title FROM videos WHERE md5_checksum = ? AND original_id IS NULL ORDER BY id ASC",
                        (md5_val,),
                    ).fetchall()

                    if len(orig_rows) > 1:
                        primary_id = orig_rows[0]["id"]
                        primary_title = orig_rows[0]["title"]
                        print(f"  - Оригиналом остается '{primary_title}' (ID: {primary_id})")

                        for row in orig_rows[1:]:
                            dup_id = row["id"]
                            dup_title = row["title"]
                            print(f"  - Переводим '{dup_title}' (ID: {dup_id}) в дубликаты...")

                            chunk_rows = conn.execute("SELECT id FROM chunks WHERE video_id = ?", (dup_id,)).fetchall()
                            chunk_ids = [r["id"] for r in chunk_rows]
                            if chunk_ids:
                                try:
                                    self.manticore.delete(collection_name=self.q_settings.table_name, ids=chunk_ids)
                                    print(f"    - Удалено {len(chunk_ids)} векторов из Manticore")
                                except Exception as me:
                                    print(f"    - Ошибка удаления из Manticore: {me}")
                                conn.execute("DELETE FROM chunks WHERE video_id = ?", (dup_id,))

                            conn.execute(
                                "UPDATE videos SET original_id = ?, status = 'skipped_duplicate_md5', "
                                "size_bytes = NULL, duration_sec = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (primary_id, dup_id),
                            )
                            print(f"    - Видео ID {dup_id} успешно переведено в статус дубликата.")
            else:
                print("✅ Контрольные суммы оригиналов уникальны.")

            # 6. Check for duplicate videos without a corresponding original video
            sql_orphan_dups = """
                SELECT id, title, original_id, md5_checksum, source_file_id FROM videos
                WHERE original_id IS NOT NULL AND (
                    original_id NOT IN (SELECT id FROM videos WHERE original_id IS NULL)
                )
            """
            orphan_dups = conn.execute(sql_orphan_dups).fetchall()
            if orphan_dups:
                for od in orphan_dups:
                    dup_id = od["id"]
                    dup_title = od["title"]
                    md5_val = od["md5_checksum"]
                    file_id = od["source_file_id"]
                    msg = (
                        f"Orphan duplicate video '{dup_title}' (ID:{dup_id}) "
                        f"original (ID:{od['original_id']}) is missing!"
                    )
                    print(f"❌ {msg} Восстанавливаем привязку...")
                    self.issues.append(msg)

                    real_orig = None
                    if md5_val:
                        real_orig = conn.execute(
                            "SELECT id FROM videos WHERE md5_checksum = ? AND original_id IS NULL LIMIT 1", (md5_val,)
                        ).fetchone()

                    if real_orig:
                        new_orig_id = real_orig["id"]
                        conn.execute(
                            "UPDATE videos SET original_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (new_orig_id, dup_id),
                        )
                        print(f"  - Перепривязали к реальному оригиналу ID {new_orig_id}")
                    else:
                        conn.execute(
                            "UPDATE videos SET original_id = NULL, status = 'pending', "
                            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (dup_id,),
                        )
                        conn.execute(
                            "INSERT INTO tasks (task_type, payload, status) VALUES (?, ?, ?)",
                            (
                                "stage_1_download",
                                json.dumps(
                                    {
                                        "file_id": file_id,
                                        "title": dup_title,
                                        "diarize": True,
                                    },
                                    ensure_ascii=False,
                                ),
                                "pending",
                            ),
                        )
                        print(f"  - Сделали видео ID {dup_id} оригиналом и поставили в очередь на загрузку.")
            else:
                print("✅ Все дубликаты привязаны к существующим оригиналам.")

    def check_chunks_deep(self) -> None:
        """Выполняет глубокую проверку последовательности и таймкодов чанков."""
        print("\n=== [4] ГЛУБОКАЯ ПРОВЕРКА ЦЕЛОСТНОСТИ CHUNKS ===")
        with db_connection(self.sqlite_settings) as conn:
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
                self.issues.append(msg)
            else:
                print("✅ Индексы чанков последовательны.")

            if time_logic_errors:
                msg = f"Chunk time logic errors (start >= end): {time_logic_errors}"
                print(f"❌ {msg}")
                self.issues.append(msg)
            else:
                print("✅ Таймкоды чанков логически верны.")

    def run_all(self) -> dict[str, Any]:
        """Запускает все проверки целостности последовательно."""
        worker_status = self.check_worker_status()
        if worker_status:
            return worker_status

        # 1. Проверка ФС и SQLite
        self.check_filesystem_and_sqlite()

        # 2. Проверка SQLite vs Manticore
        db_chunk_ids, _q_point_map = self.check_sqlite_vs_manticore()

        # 2.5. Проверка векторов
        self.check_vector_data(db_chunk_ids)

        # 3. Логические ошибки
        self.check_logical_and_heuristics()

        # 4. Глубокая проверка чанков
        self.check_chunks_deep()

        # Сохранение ошибок в БД
        try:
            with db_connection(self.sqlite_settings) as conn:
                conn.execute("DELETE FROM integrity_issues")
                for issue in self.issues:
                    conn.execute("INSERT INTO integrity_issues (message) VALUES (?)", (issue,))
        except Exception as db_err:
            print(f"❌ Failed to save integrity issues to database: {db_err}")

        summary_text = format_integrity_issues_for_telegram(self.issues)
        print("\n=== ПРОВЕРКА ЗАВЕРШЕНА ===")

        return {
            "status": "completed",
            "issues": self.issues,
            "summary": summary_text,
            "deleted_raw_count": self.deleted_raw_count,
            "deleted_norm_count": self.deleted_norm_count,
            "reindexed_videos_count": self.reindexed_videos_count,
            "reindexed_chunks_count": self.reindexed_chunks_count,
            "deleted_manticore_points_count": self.deleted_manticore_points_count,
        }


def verify_integrity(*, apply: bool = False) -> dict[str, Any]:
    """Run legacy repairs only after an explicit operator opt-in."""
    if not apply:
        raise RuntimeError(
            "This legacy script changes SQLite, Manticore, files, and tasks. "
            "Use verify_integrity_readonly.py for audits or pass --apply explicitly."
        )
    checker = IntegrityChecker()
    return checker.run_all()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Legacy integrity repair tool (mutating).")
    parser.add_argument("--apply", action="store_true", help="Actually apply repairs and destructive cleanup.")
    args = parser.parse_args()
    if not args.apply:
        parser.error("refusing to modify data without --apply; use verify_integrity_readonly.py for checks")
    res = verify_integrity(apply=True)
    print("INTEGRITY_ISSUES:" + json.dumps(res))
