import asyncio
import sys
from pathlib import Path

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_sqlite_settings
from app.db import db_connection
from app.search import hybrid_search


async def test_final_search():
    print("--- Тестирование гибридного поиска (BGE-M3 Cloud + Qdrant) ---")
    pg_settings = get_sqlite_settings()

    try:
        with db_connection(pg_settings) as conn:
            query = "углубление"
            results = await hybrid_search(conn, query, limit=3)

            print(f"Запрос: '{query}'")
            print(f"Найдено результатов: {len(results)}")

            for i, res in enumerate(results):
                print(f"\nРезультат #{i + 1}:")
                print(f"  Текст: {res.text[:100]}...")
                print(f"  Combined Score: {res.combined_score:.4f}")
                # Semantic and Lexical scores are 0.0 in RRF version as it uses ranks
                print(f"  Rank-based RRF Score: {res.combined_score:.4f}")

    except Exception as e:
        print(f"❌ Ошибка при поиске: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_final_search())
