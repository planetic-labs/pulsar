import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_manticore_settings, get_sqlite_settings
from app.manticore import get_manticore_client, init_manticore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def index_sqlite_to_manticore(manticore, table_name: str, db_path: str | Path):
    """Reusable function to index all SQLite chunks to Manticore Search."""
    logger.info("Fetching chunks from SQLite...")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql = """
            SELECT
                c.id as chunk_id, c.video_id, c.chunk_index,
                c.start_sec, c.end_sec, c.text,
                v.title, v.source_file_id, v.source_url, v.is_short, v.is_4k, v.recorded_date
            FROM chunks c
            JOIN videos v ON v.id = c.video_id
            ORDER BY c.id ASC
        """
        cursor = conn.execute(sql)

        import gc

        batch_size = 1000  # Оптимальный размер пачки для быстрой bulk-обработки
        total_indexed = 0

        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            # Собираем пачку документов в формате, который ожидает manticore.upsert
            points_to_insert = []
            for r in rows:
                points_to_insert.append(
                    {
                        "id": r["chunk_id"],
                        "payload": {
                            "chunk_id": int(r["chunk_id"]),
                            "video_id": str(r["video_id"]),
                            "chunk_index": int(r["chunk_index"]) if r["chunk_index"] is not None else 0,
                            "start_sec": float(r["start_sec"]) if r["start_sec"] is not None else 0.0,
                            "end_sec": float(r["end_sec"]) if r["end_sec"] is not None else 0.0,
                            "text": r["text"] or "",
                            "title": r["title"] or "",
                            "source_file_id": r["source_file_id"] or "",
                            "source_url": r["source_url"] or "",
                            "recorded_date": r["recorded_date"] or "",
                            "is_short": 1 if r["is_short"] else 0,
                            "is_4k": 1 if r["is_4k"] else 0,
                            "is_primary": 1,
                        },
                    }
                )

            # Отправляем пачку через наш готовый и проверенный HTTP JSON метод
            try:
                manticore.upsert(table_name, points_to_insert)
                total_indexed += len(rows)
                logger.info(f"Successfully indexed {total_indexed} chunks.")

                # Принудительно сбрасываем чанки HNSW на диск каждые 20 000 записей
                # для освобождения оперативной памяти от временного HNSW графа
                if total_indexed % 20000 == 0:
                    logger.info("Flushing Manticore tables to release HNSW RAM graph...")
                    manticore._execute_ddl("FLUSH TABLES")
            except Exception as e:
                logger.error(f"Failed to insert batch to Manticore: {e}")
                raise
            finally:
                # Очищаем ссылки и форсируем освобождение памяти
                points_to_insert = None
                rows = None
                gc.collect()

    logger.info("All data loaded successfully into Manticore Search!")


async def main():
    sqlite_settings = get_sqlite_settings()
    m_settings = get_manticore_settings()

    # 1. Инициализация клиента и удаление старой таблицы
    manticore = get_manticore_client()
    table_name = m_settings.table_name.lower()  # Гарантируем нижний регистр

    logger.info(f"Dropping existing table '{table_name}'...")
    manticore.delete_collection(table_name)

    # 2. Создание новой таблицы с правильной схемой
    logger.info("Initializing Manticore table...")
    init_manticore()

    logger.info("Waiting for table to bake...")
    await asyncio.sleep(2)

    # 3. Индексация
    await index_sqlite_to_manticore(manticore, table_name, sqlite_settings.db_path)


if __name__ == "__main__":
    asyncio.run(main())
