import sys
from pathlib import Path

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_sqlite_settings
from app.db import db_connection
from app.search import hybrid_search


def test_final_search():
    print("--- Тестирование гибридного поиска (BGE-M3 Cloud + Qdrant) ---")
    pg_settings = get_sqlite_settings()

    try:
        with db_connection(pg_settings) as conn:
            query = "углубление"
            results = hybrid_search(conn, query, limit=3)

            print(f"Запрос: '{query}'")
            print(f"Найдено результатов: {len(results)}")

            for i, res in enumerate(results):
                print(f"\nРезультат #{i + 1}:")
                print(f"  Текст: {res.text[:100]}...")
                print(f"  Combined Score: {res.combined_score:.4f}")
                print(f"  Semantic Score (S): {res.semantic_score:.4f}")
                print(f"  Lexical Score (K): {res.lexical_score:.4f}")

                if res.semantic_score > 0 and res.lexical_score > 0:
                    print("  ✅ Гибридный поиск работает (оба скора > 0)")
                elif res.semantic_score > 0:
                    print("  ⚠️ Работает только семантика")
                elif res.lexical_score > 0:
                    print("  ⚠️ Работает только лексика")
                else:
                    print("  ❌ Ошибочные оценки")

    except Exception as e:
        print(f"❌ Ошибка при поиске: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    test_final_search()
