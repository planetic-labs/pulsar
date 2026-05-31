import logging
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_sqlite_settings
from app.qdrant import get_qdrant_client
from app.voice import extract_speaker_embedding

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def execute_with_retry(db_path, query, params=(), retries=5, delay=5):
    """Выполняет запрос к SQLite с ретраями при блокировке."""
    for i in range(retries):
        try:
            with sqlite3.connect(db_path, timeout=30.0) as conn:
                conn.execute(query, params)
                conn.commit()
                return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < retries - 1:
                logger.warning(f"   База заблокирована, попытка {i + 1}/{retries} через {delay}с...")
                time.sleep(delay)
            else:
                raise e
    return False


def reprocess_all_speakers():
    settings = get_sqlite_settings()

    # Принудительно меняем qdrant -> localhost для работы вне докера
    qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    if "://qdrant" in qdrant_url:
        qdrant_url = qdrant_url.replace("://qdrant", "://localhost")
    os.environ["QDRANT_URL"] = qdrant_url

    logger.info(f"Using Qdrant URL: {qdrant_url}")
    q_client = get_qdrant_client()

    # Повышаем порог до 0.96 для максимальной точности
    threshold = 0.96

    # Сначала просто читаем список видео, не держа транзакцию
    videos = []
    with sqlite3.connect(settings.db_path, timeout=60.0) as conn:
        conn.row_factory = sqlite3.Row
        videos = conn.execute(
            "SELECT id, title, local_audio_path FROM videos WHERE local_audio_path IS NOT NULL"
        ).fetchall()

    logger.info(f"Найдено видео для обработки: {len(videos)}")

    for video in videos:
        v_id = video["id"]
        from app.config import get_app_settings

        app_settings = get_app_settings()
        audio_path = app_settings.resolve_path(video["local_audio_path"])

        if audio_path is None or not audio_path.exists():
            logger.warning(f"Файл {audio_path} не найден на диске, пропускаю {video['title']}")
            continue

        logger.info(f"--- Обработка: {video['title']} (ID: {v_id})")

        # Читаем чанки для этого видео
        chunks = []
        with sqlite3.connect(settings.db_path, timeout=60.0) as conn:
            conn.row_factory = sqlite3.Row
            chunks = conn.execute(
                "SELECT start_sec, end_sec, speaker_tags FROM chunks WHERE video_id = ? AND speaker_tags IS NOT NULL",
                (v_id,),
            ).fetchall()

        speaker_samples = {}
        for c in chunks:
            if c["speaker_tags"]:
                for tag in c["speaker_tags"].split(", "):
                    tag = tag.strip()
                    if not tag:
                        continue
                    if tag not in speaker_samples:
                        speaker_samples[tag] = []
                    speaker_samples[tag].append(c)

        if not speaker_samples:
            logger.info("   Спикеры не найдены в транскрипции.")
            continue

        for tag, chunks_list in speaker_samples.items():
            best_chunk = max(chunks_list, key=lambda x: x["end_sec"] - x["start_sec"])
            chunk_start = best_chunk["start_sec"]
            chunk_end = best_chunk["end_sec"]
            chunk_duration = chunk_end - chunk_start

            if chunk_duration > 20.0:
                mid = chunk_start + (chunk_duration / 2)
                start = max(chunk_start, mid - 10.0)
                end = min(chunk_end, start + 20.0)
            else:
                start = chunk_start
                end = chunk_end

            try:
                logger.info(f"   Спикер {tag}: анализ {start:.1f}s - {end:.1f}s...")
                embedding = extract_speaker_embedding(audio_path, start, end)

                if embedding:
                    results = q_client.query_points(collection_name="speaker_registry", query=embedding, limit=1).points

                    if results:
                        score = float(results[0].score)
                        payload = results[0].payload
                        matched_name = payload.get("name") if payload else "Unknown"
                        if score >= threshold:
                            logger.info(f"   Успех! Спикер {tag} узнан как '{matched_name}' (Score: {score:.3f})")
                            execute_with_retry(
                                settings.db_path,
                                """
                                INSERT INTO speakers (video_id, speaker_tag, name)
                                VALUES (?, ?, ?)
                                ON CONFLICT(video_id, speaker_tag) DO UPDATE SET name = EXCLUDED.name
                                """,
                                (v_id, tag, matched_name),
                            )
                        else:
                            logger.info(f"   Спикер {tag} не узнан (Score: {score:.3f} < {threshold})")
                            execute_with_retry(
                                settings.db_path,
                                "DELETE FROM speakers WHERE video_id = ? AND speaker_tag = ?",
                                (v_id, tag),
                            )
                    else:
                        logger.info(f"   Спикер {tag}: совпадений в базе нет.")
                        execute_with_retry(
                            settings.db_path, "DELETE FROM speakers WHERE video_id = ? AND speaker_tag = ?", (v_id, tag)
                        )
            except Exception as e:
                logger.error(f"   Ошибка при анализе спикера {tag}: {e}")

    logger.info("ПЕРЕОПРЕДЕЛЕНИЕ СПИКЕРОВ (ПОРОГ 0.9) ЗАВЕРШЕНО.")


if __name__ == "__main__":
    import sqlite3

    reprocess_all_speakers()
