import logging
import os
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_sqlite_settings
from app.qdrant import get_qdrant_client
from app.voice import extract_speaker_embedding

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def test_brute_force(video_id: int):
    settings = get_sqlite_settings()

    # Принудительно меняем qdrant -> localhost для работы вне докера
    qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    if "://qdrant" in qdrant_url:
        qdrant_url = qdrant_url.replace("://qdrant", "://localhost")
    os.environ["QDRANT_URL"] = qdrant_url

    print(f"Using Qdrant URL: {qdrant_url}")
    q_client = get_qdrant_client()

    # 1. Получаем инфо о видео
    with sqlite3.connect(settings.db_path) as conn:
        conn.row_factory = sqlite3.Row
        video = conn.execute("SELECT title, local_audio_path FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not video:
            print(f"Видео {video_id} не найдено")
            return

        audio_path = Path(video["local_audio_path"])
        print(f"ТЕСТ BRUTE-FORCE: {video['title']} (ID: {video_id})")

        # 2. Получаем все чанки
        chunks = conn.execute(
            "SELECT start_sec, end_sec, speaker_tags, text FROM chunks WHERE video_id = ? AND speaker_tags IS NOT NULL",
            (video_id,),
        ).fetchall()

    # Группируем чанки по тегам
    speaker_groups = {}
    for c in chunks:
        for tag in c["speaker_tags"].split(", "):
            tag = tag.strip()
            if not tag:
                continue
            if tag not in speaker_groups:
                speaker_groups[tag] = []
            speaker_groups[tag].append(c)

    print(f"Найдено спикеров (тегов): {list(speaker_groups.keys())}")

    for tag, group in speaker_groups.items():
        print(f"\n--- Анализ спикера: {tag} ({len(group)} фрагментов) ---")

        best_score = 0
        for i, c in enumerate(group):
            start, end = c["start_sec"], c["end_sec"]
            duration = end - start

            # Пропускаем совсем короткие (менее 1 сек), они часто мусорные
            if duration < 1.0:
                continue

            try:
                emb = extract_speaker_embedding(audio_path, start, end)
                if emb:
                    results = q_client.query_points(collection_name="speaker_registry", query=emb, limit=1).points

                    score = results[0].score if results else 0
                    best_score = max(best_score, score)

                    status = "✅" if score >= 0.96 else "❌"
                    print(
                        f"  [{i + 1}/{len(group)}] {start:.1f}s-{end:.1f}s | "
                        f"Score: {score:.4f} {status} | Текст: {c['text'][:40]}..."
                    )

                    if score >= 0.96:
                        print(f"  !!! НАЙДЕНО СОВПАДЕНИЕ ДЛЯ {tag} на {start} сек !!!")
                        # Можно было бы прервать, но мы хотим посмотреть все
                else:
                    print(f"  [{i + 1}/{len(group)}] Ошибка извлечения эмбеддинга")
            except Exception as e:
                print(f"  [{i + 1}/{len(group)}] Ошибка: {e}")

        print(f"ИТОГ ДЛЯ {tag}: Максимальный Score = {best_score:.4f}")


if __name__ == "__main__":
    # Тестируем на видео ID 3 (или измените на нужное)
    v_id = 3
    if len(sys.argv) > 1:
        v_id = int(sys.argv[1])
    test_brute_force(v_id)
