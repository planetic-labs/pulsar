#!/usr/bin/env python3
import sys
from pathlib import Path

# Добавляем корень проекта в путь поиска модулей
sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.config import get_sqlite_settings
from app.db import db_connection


def smart_clear_queue():
    """
    Умная очистка очереди воркера.
    Удаляет задачи, которые еще не начали обрабатываться (Stage 1).
    Оставляет задачи, которые уже находятся в работе (running) или
    уже прошли первый этап и ждут транскрибации/индексации,
    чтобы не тратить ресурсы на повторную загрузку.
    """
    settings = get_sqlite_settings()

    print("--- Умная очистка очереди Pulsar ---")

    with db_connection(settings) as conn:
        # 1. Считаем что есть сейчас
        total_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        # 2. Определяем логику очистки:
        # Оставляем:
        # - Любые задачи в статусе 'running' (уже в процессе)
        # - Задачи 'pending' для этапов 2 и 3 (уже скачаны, экономим трафик)

        # Удаляем:
        # - Все 'pending' для этапа 1 (еще не начинались)
        # - Все 'failed', 'skipped_no_space', 'skipped_silent' (ошибки/пропуски)
        # - Все 'completed' (история завершенных)

        sql = """
            DELETE FROM tasks
            WHERE status NOT IN ('running')
              AND NOT (status = 'pending' AND task_type IN ('stage_2_transcribe', 'stage_3_index'))
        """

        res = conn.execute(sql)
        deleted_count = res.rowcount

        # 3. Проверяем, что осталось
        remaining = conn.execute(
            "SELECT task_type, status, COUNT(*) as c FROM tasks GROUP BY task_type, status"
        ).fetchall()

        print(f"Удалено записей: {deleted_count} (из {total_before})")
        if remaining:
            print("\nОставлено для завершения цикла:")
            for row in remaining:
                status_disp = row["status"]
                if status_disp == "running":
                    status_disp = "ВЫПОЛНЯЕТСЯ"
                elif status_disp == "pending":
                    status_disp = "В ОЧЕРЕДИ (уже скачано)"

                print(f"  - {row['task_type']} [{status_disp}]: {row['c']}")
        else:
            print("\nОчередь полностью пуста.")

        print("\nГотово. Воркер автоматически остановится после завершения оставшихся задач.")


if __name__ == "__main__":
    smart_clear_queue()
