from __future__ import annotations

import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_embedding_settings
from app.embeddings import UnifiedEmbeddingClient


def main() -> None:
    print("Загрузка настроек эмбеддингов...")
    settings = get_embedding_settings()

    print("Текущие настройки:")
    print(f"  - Провайдер: {settings.provider}")
    print(f"  - API URL:   {settings.api_url}")
    print(f"  - Модель:    {settings.model_id}")
    print(f"  - Размерность: {settings.dimension}")
    print(f"  - LRU кэш:   {settings.cache_lru_size}")
    print("-" * 40)

    if not settings.api_url:
        print("Ошибка: EMBEDDING_API_URL не задан в переменных окружения / .env")
        sys.exit(1)

    print("Инициализация клиента...")
    client = UnifiedEmbeddingClient(settings)

    test_text = "Проверка работы нового модуля эмбеддингов."
    print(f"Отправка тестового текста: '{test_text}'")

    try:
        # Сбросим кэш L1, чтобы гарантировать реальный запрос к API
        from app.embeddings.client import clear_l1_cache

        clear_l1_cache()

        dense, sparse = client.embed_text(test_text)
        print("\nУспешное получение эмбеддинга!")
        print(f"  - Размерность dense-вектора: {len(dense)}")
        print(f"  - Первые 5 значений: {dense[:5]}")
        print(f"  - Наличие sparse-вектора: {'Да' if sparse is not None else 'Нет'}")
        if sparse is not None:
            print(f"    - Количество индексов: {len(sparse.indices)}")
            print(f"    - Первые 5 индексов: {sparse.indices[:5]}")
            print(f"    - Первые 5 значений: {sparse.values[:5]}")

    except Exception as e:
        print(f"\nОшибка при генерации эмбеддинга: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
