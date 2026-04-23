import sys
from pathlib import Path

import httpx

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_embedding_settings


def try_force_sparse():
    settings = get_embedding_settings()
    url = f"{settings.api_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    # В Infinity для получения Sparse иногда нужно передать model-id с суффиксом в самом запросе
    payloads = [
        {"model": "BAAI/bge-m3", "input": ["тест разреженных векторов"], "extra_body": {"token_embeddings": True}},
        {"model": "BAAI/bge-m3", "input": ["тест разреженных векторов"], "extra_body": {"lexical_weights": True}},
    ]

    with httpx.Client(timeout=10.0) as client:
        for i, p in enumerate(payloads):
            print(f"\n--- Тест #{i + 1} ---")
            r = client.post(url, json=p, headers=headers)
            print(f"Статус: {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                item = data["data"][0]
                print(f"Доступные ключи в ответе: {list(item.keys())}")
                if "usage" in data:
                    print(f"Ключи в usage: {list(data['usage'].keys())}")


if __name__ == "__main__":
    try_force_sparse()
