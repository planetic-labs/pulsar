import logging
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_sqlite_settings
from app.db import db_connection

logger = logging.getLogger("cleanup_tasks")


def cleanup_tasks(days: int = 14) -> int:
    """Удаляет завершенные, пропущенные и ошибочные задачи старше указанного количества дней.

    Возвращает количество удаленных записей.
    """
    settings = get_sqlite_settings()
    # Note: sqlite uses standard string formatting or parameter substitution.
    # To prevent any issues, we construct the interval string.
    interval_str = f"-{days} days"

    with db_connection(settings) as conn:
        cursor = conn.execute(
            """
            DELETE FROM tasks
            WHERE status IN ('completed', 'failed', 'skipped_silent', 'skipped_duplicate_md5', 'skipped_no_space')
              AND updated_at < datetime('now', ?)
            """,
            (interval_str,),
        )
        deleted_count = cursor.rowcount

    logger.info(f"Tasks cleanup: deleted {deleted_count} tasks updated older than {days} days.")
    return deleted_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    cleanup_tasks()
