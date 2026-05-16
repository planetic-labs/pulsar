import sys
from pathlib import Path

import httpx

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_embedding_settings


def test_sparse_activation():
    settings = get_embedding_settings()
    url = f"{settings.api_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    # Попытка №1: Обычный запрос (мы знаем, что он не дает sparse)
    # Попытка №2: Запрос с попыткой "разбудить" sparse через v2

    payloads = [
        # Попытка через стандартный OpenAI формат
        {"model": "BAAI/bge-m3", "input": ["test"], "extra_body": {"include_sparse": True}},
        # Попытка через Infinity специфичные параметры
        {"model": "BAAI/bge-m3", "input": ["test"], "extra_usage": True},
    ]

    with httpx.Client(timeout=10.0) as client:
        for i, payload in enumerate(payloads):
            print(f"\nТест #{i + 1} с payload: {payload.keys()}")
            r = client.post(url, json=payload, headers=headers)
            print(f"Статус: {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                if "data" in data and "embeddings_sparse" in data["data"][0]:
                    print("✅ СПАРС НАЙДЕН в объекте данных!")
                elif "usage" in data and "embeddings_sparse" in data["usage"]:
                    print("✅ СПАРС НАЙДЕН в usage!")
                else:
                    print("❌ Спарса всё еще нет.")


if __name__ == "__main__":
    test_sparse_activation()
