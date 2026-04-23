
import os
import httpx
import json
from pathlib import Path
import sys

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_embedding_settings

def inspect_raw_response():
    settings = get_embedding_settings()
    url = f"{settings.api_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    
    # Для BGE-M3 иногда нужно передать дополнительные параметры
    payload = {
        "model": "BAAI/bge-m3",
        "input": ["Привет мир, это тест разреженных векторов!"],
    }
    
    print(f"Отправка запроса на {url}...")
    
    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, json=payload, headers=headers)
        print(f"Статус: {response.status_code}")
        
        data = response.json()
        
        # Выводим ключи первого элемента, чтобы понять структуру
        if "data" in data and len(data["data"]) > 0:
            first_item = data["data"][0]
            print("\nКлючи в ответе одного элемента:")
            print(list(first_item.keys()))
            
            # Ищем что-то похожее на sparse
            sparse_candidates = [k for k in first_item.keys() if "sparse" in k.lower()]
            if sparse_candidates:
                print(f"\nНайдено подозрение на Sparse в ключах: {sparse_candidates}")
            else:
                print("\nВ самом объекте данных Sparse не найден.")
                
            # Проверяем поле 'usage', Infinity иногда кладет туда дополнительные данные
            if "usage" in data:
                print("\nСодержимое поля 'usage':")
                print(list(data["usage"].keys()))
                if "embeddings_sparse" in data["usage"]:
                     print("✅ НАЙДЕНО в usage['embeddings_sparse']!")
            
            # Выводим часть данных для анализа (обрезаем длинный dense-вектор)
            debug_item = first_item.copy()
            if "embedding" in debug_item:
                debug_item["embedding"] = f"[{len(debug_item['embedding'])} floats...]"
            
            print("\nПример структуры данных (без векторов):")
            print(json.dumps(debug_item, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    inspect_raw_response()
