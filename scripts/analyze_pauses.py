#!/usr/bin/env python3
"""
Анализирует распределение пауз в JSON-файлах Deepgram.
Адаптирован под структуру хранения Pulsar.
Запуск:
  python3 scripts/analyze_pauses.py
  python3 scripts/analyze_pauses.py <path_to_json_or_directory>
  python3 scripts/analyze_pauses.py --limit 100 --source normalized
"""

import argparse
import gzip
import json
import sys
from collections import Counter
from pathlib import Path

# Добавляем корневую директорию проекта в пути импорта
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_app_settings, get_sqlite_settings
from app.db import db_connection


def get_adjusted_settings() -> tuple[Path, Path, Path]:
    """
    Возвращает (db_path, raw_transcripts_dir, normalized_transcripts_dir)
    с автоматической корректировкой путей, если запуск происходит на хосте (вне Docker).
    """
    root_dir = Path(__file__).resolve().parent.parent

    app_settings = get_app_settings()
    sqlite_settings = get_sqlite_settings()

    db_path = sqlite_settings.db_path
    raw_dir = app_settings.raw_transcripts_dir
    norm_dir = app_settings.normalized_transcripts_dir

    # Если запуск вне Docker (нет папки /app), но пути указывают на /app
    if not Path("/app").exists():
        if str(db_path).startswith("/app"):
            db_path = root_dir / db_path.relative_to("/app")
        if str(raw_dir).startswith("/app"):
            raw_dir = root_dir / raw_dir.relative_to("/app")
        if str(norm_dir).startswith("/app"):
            norm_dir = root_dir / norm_dir.relative_to("/app")

    return db_path, raw_dir, norm_dir


def get_transcript_path(source_file_id: str, base_dir: Path) -> Path:
    """Строит путь к файлу транскрипта с учетом шардирования."""
    prefix = source_file_id[:2] if len(source_file_id) >= 2 else source_file_id
    return base_dir / prefix / f"{source_file_id}.json.gz"


def analyze_file(filepath: Path) -> tuple[list[float], list[float], list[dict]] | None:
    """
    Считывает транскрипт (поддерживает обычный JSON и .json.gz) и возвращает:
    - список всех пауз (в секундах)
    - список пауз на стыке смены спикеров (в секундах)
    - исходный список реплик (utterances)
    """
    try:
        if filepath.suffix == ".gz":
            with gzip.open(filepath, "rt", encoding="utf-8") as f:
                data = json.load(f)
        else:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        print(f"Ошибка чтения файла {filepath.name}: {e}", file=sys.stderr)
        return None

    # Пытаемся достать utterances сначала из results.utterances (сырой формат Deepgram)
    results = data.get("results", {})
    utterances = results.get("utterances", [])

    # Если не нашли, то ищем в корне (нормализованный формат)
    if not utterances:
        utterances = data.get("utterances", [])

    if len(utterances) < 2:
        return None

    pauses: list[float] = []
    speaker_changes: list[float] = []

    for i in range(1, len(utterances)):
        prev = utterances[i - 1]
        curr = utterances[i]

        try:
            prev_end = float(prev.get("end", 0.0))
            curr_start = float(curr.get("start", 0.0))
        except (ValueError, TypeError):
            continue

        pause = round(curr_start - prev_end, 3)
        if pause < 0:
            pause = 0.0  # overlap — считаем как 0
        pauses.append(pause)

        # Проверяем смену спикера, если они заданы
        prev_speaker = prev.get("speaker")
        curr_speaker = curr.get("speaker")
        if prev_speaker is not None and curr_speaker is not None:
            if curr_speaker != prev_speaker:
                speaker_changes.append(pause)

    return pauses, speaker_changes, utterances


def bucket(pause: float) -> str:
    """Группирует паузы по интервалам для построения гистограммы."""
    if pause < 0.5:
        return "<0.5s"
    if pause < 1.0:
        return "0.5–1s"
    if pause < 2.0:
        return "1–2s"
    if pause < 3.0:
        return "2–3s"
    if pause < 5.0:
        return "3–5s"
    if pause < 10.0:
        return "5–10s"
    return "10s+"


def speaker_durations(utterances: list[dict]) -> dict[str | int, float]:
    """Считает суммарное время говорения для каждого спикера."""
    durations: dict[str | int, float] = {}
    for u in utterances:
        s = u.get("speaker")
        if s is None:
            continue
        try:
            start = float(u.get("start", 0.0))
            end = float(u.get("end", 0.0))
            durations[s] = durations.get(s, 0.0) + (end - start)
        except (ValueError, TypeError):
            continue
    return durations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Анализирует распределение пауз в JSON-файлах Deepgram (raw или normalized)."
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=str,
        default=None,
        help="Путь к файлу .json/.json.gz или директории. Если не указан, анализирует все видео из базы данных.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Ограничение на количество анализируемых файлов (по умолчанию: без лимита).",
    )
    parser.add_argument(
        "--source",
        choices=["raw", "normalized"],
        default="raw",
        help="Тип транскрипта при чтении из БД: 'raw' (по умолчанию) или 'normalized'.",
    )
    args = parser.parse_args()

    db_path, raw_dir, norm_dir = get_adjusted_settings()

    files_to_analyze: list[Path] = []

    if args.path:
        target = Path(args.path)
        if target.is_dir():
            files_to_analyze = sorted(list(target.glob("**/*.json")) + list(target.glob("**/*.json.gz")))
        elif target.is_file():
            files_to_analyze = [target]
        else:
            print(f"Путь не найден: {args.path}", file=sys.stderr)
            sys.exit(1)
        print(f"Найдено файлов для анализа по пути: {len(files_to_analyze)}")
    else:
        print(f"Подключение к БД по пути: {db_path}")
        print("Сканирование базы данных на наличие транскрибированных видео...")
        try:
            # Создаем временные SQLite settings с скорректированным путем
            from app.config import SQLiteSettings as ConfigSQLiteSettings

            adj_sqlite_settings = ConfigSQLiteSettings(db_path=db_path)

            with db_connection(adj_sqlite_settings) as conn:
                query = """
                    SELECT id, source_file_id, title
                    FROM videos
                    WHERE original_id IS NULL
                      AND status NOT IN ('pending', 'failed', 'skipped_silent')
                    ORDER BY created_at DESC
                """
                rows = conn.execute(query).fetchall()
        except Exception as e:
            print(f"Ошибка подключения к БД или выполнения запроса: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"В БД найдено {len(rows)} оригинальных транскрибированных видео.")

        base_dir = raw_dir if args.source == "raw" else norm_dir
        alt_base_dir = norm_dir if args.source == "raw" else raw_dir

        for row in rows:
            file_id = row["source_file_id"]
            if not file_id:
                continue

            path = get_transcript_path(file_id, base_dir)
            if path.exists():
                files_to_analyze.append(path)
            else:
                # Если выбранный тип отсутствует, попробуем альтернативный
                alt_path = get_transcript_path(file_id, alt_base_dir)
                if alt_path.exists():
                    files_to_analyze.append(alt_path)

        print(f"Найдено локальных файлов транскриптов для анализа: {len(files_to_analyze)}")

    if args.limit:
        files_to_analyze = files_to_analyze[: args.limit]
        print(f"Применено ограничение в {args.limit} файлов.")

    if not files_to_analyze:
        print("Нет данных для анализа.")
        return

    all_pauses: list[float] = []
    all_speaker_change_pauses: list[float] = []
    master_detection_results: list[tuple[str | int, float]] = []
    processed_count = 0
    total_duration_sec = 0.0

    # Кэшируем результаты разбора файлов для симуляции порогов
    cached_file_results: list[tuple[Path, list[float]]] = []

    for f in files_to_analyze:
        result = analyze_file(f)
        if result is None:
            continue

        processed_count += 1
        pauses, sc_pauses, utterances = result
        all_pauses.extend(pauses)
        all_speaker_change_pauses.extend(sc_pauses)

        cached_file_results.append((f, pauses))

        # Пытаемся получить длительность аудио из метаданных файла
        try:
            if f.suffix == ".gz":
                with gzip.open(f, "rt", encoding="utf-8") as file:
                    data = json.load(file)
            else:
                with open(f, encoding="utf-8") as file:
                    data = json.load(file)
            duration = data.get("metadata", {}).get("duration", 0.0) or data.get("duration", 0.0)
            total_duration_sec += float(duration)
        except Exception:
            pass

        durations = speaker_durations(utterances)
        if durations:
            master_speaker = max(durations, key=durations.get)
            total_dur = sum(durations.values())
            master_share = durations[master_speaker] / total_dur if total_dur > 0 else 0
            master_detection_results.append((master_speaker, master_share))

    if not all_pauses:
        print("\nНет данных для анализа (все файлы пустые или содержат менее 2 реплик).")
        return

    # Распределение пауз
    counts = Counter(bucket(p) for p in all_pauses)
    order = ["<0.5s", "0.5–1s", "1–2s", "2–3s", "3–5s", "5–10s", "10s+"]

    total = len(all_pauses)
    print(f"\n=== Распределение пауз (всего пауз: {total}, файлов: {processed_count}) ===")
    cumulative = 0
    for b in order:
        n = counts.get(b, 0)
        cumulative += n
        bar = "█" * int(40 * n / total) if total else ""
        print(f"  {b:>8}  {n:>5}  {n / total * 100:>5.1f}%  (кум. {cumulative / total * 100:.0f}%)  {bar}")

    # Паузы на сменах спикера
    if all_speaker_change_pauses:
        sc_counts = Counter(bucket(p) for p in all_speaker_change_pauses)
        sc_total = len(all_speaker_change_pauses)
        print(f"\n=== Паузы на сменах спикера ({sc_total} случаев) ===")
        for b in order:
            n = sc_counts.get(b, 0)
            if n:
                print(f"  {b:>8}  {n:>5}  {n / sc_total * 100:.1f}%")
    else:
        print("\n=== Смен спикера не обнаружено (или файлы с одним спикером / без разметки спикеров) ===")

    # Определение Мастера
    if master_detection_results:
        shares = [s for _, s in master_detection_results]
        avg_share = sum(shares) / len(shares)
        clear_cases = sum(1 for s in shares if s > 0.7)
        print("\n=== Определение Мастера (доминирующий спикер) ===")
        print(f"  Средняя доля доминирующего спикера: {avg_share * 100:.1f}%")
        print(f"  Файлов с долей >70%: {clear_cases}/{len(shares)}")

    # Рекомендация порога
    print("\n=== Рекомендация порога ===")
    over_2 = sum(1 for p in all_pauses if p >= 2.0)
    over_3 = sum(1 for p in all_pauses if p >= 3.0)
    over_5 = sum(1 for p in all_pauses if p >= 5.0)
    print(f"  Пауз ≥2s: {over_2} ({over_2 / total * 100:.1f}%)")
    print(f"  Пауз ≥3s: {over_3} ({over_3 / total * 100:.1f}%)")
    print(f"  Пауз ≥5s: {over_5} ({over_5 / total * 100:.1f}%)")

    # Расчет количества чанков на час
    total_duration_hours = total_duration_sec / 3600.0
    if total_duration_hours > 0:
        print(f"\n=== Расчетное количество чанков на час записи (общая длительность: {total_duration_hours:.2f} ч) ===")
        for threshold in [2.0, 3.0, 4.0, 5.0, 7.0, 10.0]:
            total_chunks = 0
            for _, pauses in cached_file_results:
                # Количество чанков = (число пауз в файле >= threshold) + 1
                total_chunks += sum(1 for p in pauses if p >= threshold) + 1

            chunks_per_hour = total_chunks / total_duration_hours
            print(f"  При пороге ≥{threshold}s: ~{chunks_per_hour:.1f} чанков/час (всего чанков: {total_chunks})")

    print("\n  Запусти на реальных файлах и смотри: при каком пороге")
    print("  количество чанков на час записи выглядит разумно (10–40 шт).")


if __name__ == "__main__":
    main()
