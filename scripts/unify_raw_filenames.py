import sys
from pathlib import Path

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_app_settings, get_sqlite_settings
from app.db import db_connection


def unify_filenames():
    app_settings = get_app_settings()
    sqlite_settings = get_sqlite_settings()
    raw_base_dir = app_settings.raw_transcripts_dir

    print(f"Base raw directory: {raw_base_dir}")

    count = 0
    with db_connection(sqlite_settings) as conn:
        # Получаем все транскрипты, чтобы знать, какие видео у нас есть и где их файлы
        transcripts = conn.execute("""
            SELECT t.id, t.video_id, t.raw_json_path, v.source_file_id
            FROM transcripts t
            JOIN videos v ON v.id = t.video_id
        """).fetchall()

        for t in transcripts:
            t_id = t["id"]
            video_id_str = t["source_file_id"]
            video_dir = raw_base_dir / video_id_str

            if not video_dir.exists() or not video_dir.is_dir():
                print(f"⚠️  Directory not found for video {video_id_str}")
                continue

            # Ищем все JSON в папке видео
            json_files = list(video_dir.glob("*.json"))
            if not json_files:
                continue

            # Целевое имя файла
            new_filename = f"dg_nova3_{video_id_str}.json"
            new_path = video_dir / new_filename

            # Выбираем лучший файл-источник (самый свежий)
            # Если целевой файл уже существует под правильным именем, он тоже будет в списке
            latest_source = max(json_files, key=lambda x: x.stat().st_mtime)

            # Если самый свежий и есть наш целевой - просто обновляем путь в БД (на всякий случай)
            # Если самый свежий имеет другое имя - переименовываем
            if latest_source.name != new_filename:
                print(f"Renaming: {latest_source.name} -> {new_filename}")
                # Если файл с таким именем уже был (но он старее), удалим его или переименуем
                if new_path.exists():
                    backup_name = f"{new_filename}.old_{int(new_path.stat().st_mtime)}"
                    new_path.rename(video_dir / backup_name)

                latest_source.rename(new_path)

            # Обновляем путь в базе данных (всегда используем абсолютный путь для консистентности)
            abs_new_path = str(new_path.resolve())
            conn.execute("UPDATE transcripts SET raw_json_path = ? WHERE id = ?", (abs_new_path, t_id))
            count += 1

    print(f"\nSuccessfully unified {count} transcript paths and filenames.")


if __name__ == "__main__":
    unify_filenames()
