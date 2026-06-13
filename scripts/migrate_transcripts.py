#!/usr/bin/env python3
"""Скрипт для миграции, архивации и шардирования файлов транскрипций.

Переносит файлы из старого формата хранения в новый:
- Raw: storage/transcripts/raw/{source_file_id}/dg_nova3_{source_file_id}.json
  -> storage/transcripts/raw/{prefix}/{source_file_id}.json.gz
- Normalized: storage/transcripts/normalized/{source_file_id}_deepgram.json
  -> storage/transcripts/normalized/{prefix}/{source_file_id}.json.gz

Где {prefix} - первые два символа от source_file_id.
Файлы сжимаются в gzip с удалением отступов (компактный JSON).
При наличии дубликатов выбирается самый свежий файл по времени изменения (mtime).
"""

import gzip
import json
import shutil
import sys
from pathlib import Path

# Добавляем корень проекта в пути поиска модулей
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_app_settings

# Дополнительно инициализируем настройки
settings = get_app_settings()


def extract_source_file_id_raw(path: Path, raw_dir: Path) -> str:
    """Извлекает source_file_id для raw транскрипта."""
    # Если файл лежит в поддиректории, то имя этой директории - это source_file_id
    if path.parent != raw_dir:
        return path.parent.name

    # Иначе парсим из имени файла
    name = path.name
    if name.startswith("dg_nova3_"):
        name = name[len("dg_nova3_") :]
    if name.endswith(".json.gz"):
        name = name[:-8]
    elif name.endswith(".json"):
        name = name[:-5]
    return name


def extract_source_file_id_normalized(path: Path) -> str:
    """Извлекает source_file_id для normalized транскрипта."""
    name = path.name
    if name.endswith(".json.gz"):
        name = name[:-8]
    elif name.endswith(".json"):
        name = name[:-5]

    if name.endswith("_deepgram"):
        name = name[:-9]
    return name


def get_target_prefix(source_file_id: str) -> str:
    """Возвращает двухсимвольный префикс для шардирования."""
    if len(source_file_id) >= 2:
        return source_file_id[:2]
    return source_file_id


def load_and_clean_json(path: Path) -> dict | None:
    """Загружает JSON из файла (поддерживает обычный и gzip) и проверяет валидность."""
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        else:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"Ошибка чтения/парсинга файла {path}: {e}", file=sys.stderr)
        return None


def save_compressed_json(data: dict, target_path: Path) -> None:
    """Сохраняет данные в компактный json.gz файл."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target_path, "wt", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)


def migrate_category(src_dir: Path, temp_dir: Path, is_raw: bool) -> tuple[int, int]:
    """Выполняет миграцию одной категории файлов (raw или normalized) во временную папку.

    Возвращает кортеж (успешно_перенесено, пропущено_дубликатов).
    """
    category_name = "raw" if is_raw else "normalized"
    print(f"Сканирование файлов в директории {src_dir}...")

    # Ищем все .json и .json.gz файлы
    glob_pattern = "**/*.json*" if is_raw else "*.json*"
    all_files = [p for p in src_dir.glob(glob_pattern) if p.suffix in (".json", ".gz")]

    print(f"Найдено файлов для обработки: {len(all_files)}")

    # Группируем по source_file_id
    grouped_files: dict[str, list[Path]] = {}
    for p in all_files:
        # Пропускаем файлы, которые уже лежат в правильном месте
        # (например, если скрипт запущен повторно и пути пересекаются)
        rel_path = p.relative_to(src_dir)
        if len(rel_path.parts) >= 2 and len(rel_path.parts[0]) == 2:
            # Похоже на уже шардированный путь, пропустим его
            continue

        if is_raw:
            file_id = extract_source_file_id_raw(p, src_dir)
        else:
            file_id = extract_source_file_id_normalized(p)

        if not file_id:
            continue

        grouped_files.setdefault(file_id, []).append(p)

    migrated_count = 0
    skipped_duplicates = 0

    # Переносим лучшие файлы
    for file_id, paths in grouped_files.items():
        # Если путей несколько, выбираем самый свежий по времени изменения
        if len(paths) > 1:
            # Сортируем по mtime по убыванию
            paths.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            chosen_path = paths[0]
            skipped_duplicates += len(paths) - 1
            print(
                f"Дубликат для {file_id}: выбрано {chosen_path.name} (mtime: {chosen_path.stat().st_mtime}), "
                f"пропущено {len(paths) - 1} файлов."
            )
        else:
            chosen_path = paths[0]

        # Загружаем JSON
        data = load_and_clean_json(chosen_path)
        if data is None:
            print(f"Пропуск поврежденного файла: {chosen_path}", file=sys.stderr)
            continue

        # Формируем новый путь во временной папке
        prefix = get_target_prefix(file_id)
        target_path = temp_dir / category_name / prefix / f"{file_id}.json.gz"

        # Сохраняем в сжатом компактном виде
        save_compressed_json(data, target_path)
        migrated_count += 1

        if migrated_count % 500 == 0:
            print(f"Обработано {migrated_count} файлов...")

    return migrated_count, skipped_duplicates


def main() -> None:
    raw_src = settings.raw_transcripts_dir
    norm_src = settings.normalized_transcripts_dir

    temp_dir = settings.storage_dir / "transcripts_temp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    print("=== НАЧАЛО МИГРАЦИИ ТРАНСКРИПТОВ ===")

    # 1. Миграция RAW
    raw_migrated, raw_skipped = migrate_category(raw_src, temp_dir, is_raw=True)
    print(f"RAW: успешно перенесено: {raw_migrated}, дубликатов пропущено: {raw_skipped}")

    # 2. Миграция Normalized
    norm_migrated, norm_skipped = migrate_category(norm_src, temp_dir, is_raw=False)
    print(f"Normalized: успешно перенесено: {norm_migrated}, дубликатов пропущено: {norm_skipped}")

    # 3. Безопасная замена директорий
    print("Замена старых папок на новые...")

    # Резервные копии на случай непредвиденных ошибок при переименовании
    backup_dir = settings.storage_dir / "transcripts_backup"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Перемещаем старые папки в бэкап
        if raw_src.exists():
            shutil.move(str(raw_src), str(backup_dir / "raw"))
        if norm_src.exists():
            shutil.move(str(norm_src), str(backup_dir / "normalized"))

        # Создаем целевые папки заново (так как мы переместили их родителей в backup)
        raw_src.parent.mkdir(parents=True, exist_ok=True)

        # Перемещаем новые папки из temp в постоянное хранилище
        if (temp_dir / "raw").exists():
            shutil.move(str(temp_dir / "raw"), str(raw_src))
        else:
            raw_src.mkdir(parents=True, exist_ok=True)

        if (temp_dir / "normalized").exists():
            shutil.move(str(temp_dir / "normalized"), str(norm_src))
        else:
            norm_src.mkdir(parents=True, exist_ok=True)

        # Удаляем временную папку и бэкап
        shutil.rmtree(temp_dir)
        shutil.rmtree(backup_dir)
        print("Миграция успешно завершена!")

    except Exception as e:
        print(f"КРИТИЧЕСКАЯ ОШИБКА при замене директорий: {e}", file=sys.stderr)
        print("Попытка восстановления из бэкапа...", file=sys.stderr)
        # Восстановление
        if (backup_dir / "raw").exists():
            if raw_src.exists():
                shutil.rmtree(raw_src)
            shutil.move(str(backup_dir / "raw"), str(raw_src))
        if (backup_dir / "normalized").exists():
            if norm_src.exists():
                shutil.rmtree(norm_src)
            shutil.move(str(backup_dir / "normalized"), str(norm_src))
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        sys.exit(1)


if __name__ == "__main__":
    main()
