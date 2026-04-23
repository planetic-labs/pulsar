import sys
from pathlib import Path

import httpx

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_embedding_settings


def debug_hf_endpoints():
    settings = get_embedding_settings()
    url_base = settings.api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    print(f"--- Отладка эндпоинтов для {url_base} ---")

    with httpx.Client(timeout=10.0) as client:
        # 1. Пробуем список моделей
        try:
            r = client.get(f"{url_base}/models", headers=headers)
            print(f"GET /models: {r.status_code}")
            if r.status_code == 200:
                print(f"Модели: {r.json()}")
        except Exception as e:
            print(f"GET /models failed: {e}")

        # 2. Пробуем /v1/embeddings (OpenAI standard)
        try:
            payload = {"model": "BAAI/bge-m3", "input": ["test"]}
            r = client.post(f"{url_base}/v1/embeddings", json=payload, headers=headers)
            print(f"POST /v1/embeddings: {r.status_code}")
            if r.status_code == 200:
                print("✅ /v1/embeddings работает!")
        except Exception as e:
            print(f"POST /v1/embeddings failed: {e}")

        # 3. Пробуем /v2/embeddings (Infinity native)
        try:
            r = client.post(f"{url_base}/v2/embeddings", json=payload, headers=headers)
            print(f"POST /v2/embeddings: {r.status_code}")
            if r.status_code == 200:
                print("✅ /v2/embeddings работает!")
        except Exception as e:
            print(f"POST /v2/embeddings failed: {e}")


if __name__ == "__main__":
    debug_hf_endpoints()
