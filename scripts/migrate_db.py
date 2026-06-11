#!/usr/bin/env python3
"""Скрипт для переноса данных из старой БД в новую с изменением структуры.

Переносит:
1. Таблицу folders (полное копирование).
2. Таблицу videos:
   - Поле processing_status переименовывается в status.
   - Выполняется поиск дубликатов по md5_checksum.
   - Первое видео в группе дубликатов (по id ASC) становится оригиналом (original_id = NULL).
   - Все последующие видео становятся дубликатами: original_id указывает на оригинал,
     status = 'skipped_duplicate_md5', size_bytes = NULL, duration_sec = NULL.
"""

import sqlite3
import sys
from pathlib import Path

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_sqlite_settings


def migrate():
    # Пути к базам данных
    old_db_path = Path("/home/devman/workspace/pulsar/data/pulsar.db")
    new_db_path = Path(get_sqlite_settings().db_path)

    if not old_db_path.exists():
        print(f"Ошибка: Старая база данных не найдена по пути {old_db_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Старая БД: {old_db_path}")
    print(f"Новая БД: {new_db_path}")

    # Соединяемся с базами данных
    old_conn = sqlite3.connect(old_db_path)
    old_conn.row_factory = sqlite3.Row

    new_conn = sqlite3.connect(new_db_path)
    new_conn.execute("PRAGMA foreign_keys = OFF;")  # Временно отключаем FK проверки для миграции

    try:
        # 1. Очищаем новую базу данных перед миграцией
        print("Очистка новой базы данных...")
        new_conn.execute("DELETE FROM videos")
        new_conn.execute("DELETE FROM folders")
        new_conn.commit()

        # 2. Переносим folders
        print("Перенос таблицы folders...")
        old_folders = old_conn.execute("SELECT id, name, parent_id, created_at FROM folders").fetchall()
        for folder in old_folders:
            new_conn.execute(
                "INSERT INTO folders (id, name, parent_id, created_at) VALUES (?, ?, ?, ?)",
                (folder["id"], folder["name"], folder["parent_id"], folder["created_at"]),
            )
        print(f"Перенесено папок: {len(old_folders)}")

        # 3. Загружаем все видео из старой базы
        print("Загрузка видео из старой базы...")
        old_videos = old_conn.execute(
            "SELECT id, source_file_id, parent_folder_id, md5_checksum, title, recorded_date, "
            "is_short, source_url, mime_type, size_bytes, duration_sec, processing_status, "
            "is_4k, is_missing, is_excluded, created_at, updated_at FROM videos"
        ).fetchall()

        # Группируем видео по md5_checksum для определения оригиналов и дубликатов
        # md5_checksum группируем только если он не пустой
        md5_groups = {}
        for v in old_videos:
            md5 = v["md5_checksum"]
            if md5 and md5.strip():
                md5_groups.setdefault(md5, []).append(v)

        # Словари для быстрого поиска роли видео
        # video_id -> original_id (указывает на id оригинала, или None если это оригинал)
        video_roles = {}
        for _, group in md5_groups.items():
            if len(group) > 1:
                # Сортируем по id по возрастанию
                group.sort(key=lambda x: x["id"])
                original = group[0]
                video_roles[original["id"]] = None  # Оригинал
                for duplicate in group[1:]:
                    video_roles[duplicate["id"]] = original["id"]  # Ссылка на оригинал

        # 4. Переносим видео в новую структуру
        print("Перенос таблицы videos...")
        migrated_count = 0
        original_count = 0
        duplicate_count = 0

        for v in old_videos:
            v_id = v["id"]
            original_id = video_roles.get(v_id, None)

            # Определяем значения полей в зависимости от роли (оригинал или дубликат)
            if original_id is not None:
                # Это дубликат
                status = "skipped_duplicate_md5"
                size_bytes = None
                duration_sec = None
                duplicate_count += 1
            else:
                # Это оригинал (или уникальное видео без md5)
                status = v["processing_status"]
                size_bytes = v["size_bytes"]
                duration_sec = v["duration_sec"]
                original_count += 1

            new_conn.execute(
                "INSERT INTO videos (id, source_file_id, parent_folder_id, md5_checksum, title, "
                "recorded_date, is_short, source_url, mime_type, size_bytes, duration_sec, status, "
                "is_4k, is_missing, is_excluded, original_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    v["id"],
                    v["source_file_id"],
                    v["parent_folder_id"],
                    v["md5_checksum"],
                    v["title"],
                    v["recorded_date"],
                    v["is_short"],
                    v["source_url"],
                    v["mime_type"],
                    size_bytes,
                    duration_sec,
                    status,
                    v["is_4k"],
                    v["is_missing"],
                    v["is_excluded"],
                    original_id,
                    v["created_at"],
                    v["updated_at"],
                ),
            )
            migrated_count += 1

        new_conn.commit()
        print(f"Успешно перенесено видео: {migrated_count}")
        print(f"  - Оригиналов: {original_count}")
        print(f"  - Дубликатов по MD5: {duplicate_count}")

        # Включаем FK обратно и проверяем консистентность
        new_conn.execute("PRAGMA foreign_keys = ON;")
        fk_errors = new_conn.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_errors:
            print("ВНИМАНИЕ: Обнаружены ошибки внешних ключей после миграции:", file=sys.stderr)
            for err in fk_errors:
                print(
                    f"  Таблица: {err[0]}, Строка (rowid): {err[1]}, Цель: {err[2]}, Индекс FK: {err[3]}",
                    file=sys.stderr,
                )
        else:
            print("Проверка внешних ключей пройдена успешно.")

    except Exception as e:
        new_conn.rollback()
        print(f"Критическая ошибка при миграции: {e}", file=sys.stderr)
        raise
    finally:
        old_conn.close()
        new_conn.close()


if __name__ == "__main__":
    migrate()
