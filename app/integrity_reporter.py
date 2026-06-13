from __future__ import annotations

import re


def format_integrity_issues_for_telegram(issues: list[str]) -> str:
    """Groups integrity issues by category and formats them nicely for Telegram notifications.

    Instead of listing all items, it shows the type of error and the count.
    """
    if not issues:
        return ""

    # Categories definition with description and regex pattern to match
    categories = [
        {
            "id": "zero_chunks",
            "title": "Отсутствуют чанки в БД (0 chunks)",
            "pattern": r"0 chunks in DB!",
        },
        {
            "id": "low_wpm",
            "title": "Низкая плотность слов (Low WPM)",
            "pattern": r"Low WPM density",
        },
        {
            "id": "chunk_mismatch",
            "title": "Несовпадение кол-ва чанков (DB vs expected)",
            "pattern": r"chunk count mismatch",
        },
        {
            "id": "missing_raw",
            "title": "Отсутствует исходный JSON (RAW)",
            "pattern": r"Missing RAW at",
        },
        {
            "id": "missing_norm",
            "title": "Отсутствует обработанный JSON (NORMALIZED)",
            "pattern": r"Missing NORMALIZED at",
        },
        {
            "id": "corrupted_raw",
            "title": "Поврежден исходный JSON (RAW)",
            "pattern": r"Corrupted RAW JSON",
        },
        {
            "id": "corrupted_norm",
            "title": "Поврежден обработанный JSON (NORMALIZED)",
            "pattern": r"Corrupted NORM JSON",
        },
        {
            "id": "text_mismatch",
            "title": "Рассинхронизация текста (DB vs JSON)",
            "pattern": r"Text mismatch between DB and JSON",
        },
        {
            "id": "manticore_missing",
            "title": "Чанки SQLite отсутствуют в Manticore",
            "pattern": r"Chunks in SQLite missing in Manticore",
        },
        {
            "id": "manticore_orphan",
            "title": "Лишние точки в Manticore (сироты)",
            "pattern": r"Orphan points in Manticore",
        },
        {
            "id": "manticore_text_mismatch",
            "title": "Несовпадение текста чанка (SQLite vs Manticore)",
            "pattern": r"Manticore text mismatch for chunk ID",
        },
        {
            "id": "manticore_meta_mismatch",
            "title": "Несовпадение метаданных чанка (SQLite vs Manticore)",
            "pattern": r"Manticore metadata mismatch for chunk ID",
        },
        {
            "id": "db_sequence_errors",
            "title": "Ошибки последовательности индексов чанков в SQLite",
            "pattern": r"Chunk sequence errors in DB",
        },
        {
            "id": "db_time_errors",
            "title": "Логические ошибки таймкодов чанков в SQLite",
            "pattern": r"Chunk time logic errors",
        },
        {
            "id": "orphan_raw_delete_failed",
            "title": "Ошибка удаления сиротского файла RAW",
            "pattern": r"Failed to delete orphan RAW file",
        },
        {
            "id": "orphan_norm_delete_failed",
            "title": "Ошибка удаления сиротского файла NORMALIZED",
            "pattern": r"Failed to delete orphan NORMALIZED file",
        },
        {
            "id": "manticore_query_failed",
            "title": "Сбой запроса к Manticore Search",
            "pattern": r"Manticore query failed",
        },
    ]

    grouped_counts: dict[str, int] = {c["id"]: 0 for c in categories}
    other_issues: list[str] = []

    for issue in issues:
        matched = False
        for cat in categories:
            if re.search(cat["pattern"], issue, re.IGNORECASE):
                grouped_counts[cat["id"]] += 1
                matched = True
                break
        if not matched:
            other_issues.append(issue)

    lines: list[str] = []
    lines.append("<b>⚠️ Обнаружены отклонения при проверке целостности Pulsar!</b>\n")

    for cat in categories:
        count = grouped_counts[cat["id"]]
        if count > 0:
            lines.append(f"• <b>{cat['title']}:</b> {count} шт.")

    if other_issues:
        lines.append(f"\n• <b>Другие отклонения:</b> {len(other_issues)} шт.")
        for idx, other in enumerate(other_issues[:3], 1):
            lines.append(f"   {idx}. {other}")

    return "\n".join(lines)
