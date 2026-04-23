import sys
from pathlib import Path

import httpx

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_embedding_settings


def final_test():
    settings = get_embedding_settings()
    url_base = settings.api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {"model": "BAAI/bge-m3", "input": ["test sentence"]}

    endpoints = ["/v2/embeddings", "/v1/embeddings", "/embeddings", "/embed"]

    print("--- Тестирование различных эндпоинтов ---")

    with httpx.Client(timeout=20.0) as client:
        for ep in endpoints:
            try:
                url = f"{url_base}{ep}"
                r = client.post(url, json=payload, headers=headers)
                print(f"POST {ep}: {r.status_code}")
                if r.status_code == 200:
                    print(f"🎉 НАЙДЕНО! Эндпоинт {ep} работает.")
                    data = r.json()
                    # Проверяем структуру
                    if "data" in data:
                        emb = data["data"][0]
                        print(f"Размерность: {len(emb.get('embedding', []))}")
                    return
            except Exception as e:
                print(f"POST {ep} failed: {e}")


if __name__ == "__main__":
    final_test()
