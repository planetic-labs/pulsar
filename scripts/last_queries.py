#!/usr/bin/env python3
import sys
from pathlib import Path

# Добавляем корень проекта в путь поиска модулей
sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.config import get_sqlite_settings
from app.db import db_connection


def print_last_queries() -> None:
    """Выводит 50 последних запросов из кэша базы данных."""
    settings = get_sqlite_settings()

    print("--- 50 последних запросов из кэша Pulsar ---")

    with db_connection(settings) as conn:
        # Получаем 50 последних записей
        cursor = conn.execute("SELECT id, query, created_at FROM query_cache ORDER BY id DESC LIMIT 50")
        rows = cursor.fetchall()

        if not rows:
            print("Кэш запросов пуст.")
            return

        print(f"{'ID':<6} | {'Дата и время':<19} | {'Запрос'}")
        print("-" * 80)
        for row in rows:
            # Убедимся, что created_at и query не None для корректного вывода
            created_at = row["created_at"] or ""
            query = row["query"] or ""
            print(f"{row['id']:<6} | {created_at:<19} | {query}")


if __name__ == "__main__":
    print_last_queries()
