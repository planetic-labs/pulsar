
import sys
import os
from pathlib import Path

# Добавляем корень проекта в путь
ROOT_DIR = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_embedding_settings
from app.gemini import UnifiedEmbeddingClient, GeminiSettings

def test_hf_integration():
    # Загружаем настройки (они подтянутся из .env автоматом)
    hf_settings = get_embedding_settings()
    
    # Пустой конфиг для Gemini (он нам не нужен для этого теста)
    gemini_settings = GeminiSettings(api_keys=[], embedding_model="text-embedding-004")
    
    client = UnifiedEmbeddingClient(gemini_settings, hf_settings)
    
    print(f"--- Тестирование HF Space ---")
    print(f"URL: {hf_settings.api_url}")
    print(f"Token configured: {'Yes' if hf_settings.api_token else 'No'}")
    
    if not hf_settings.api_url:
        print("Ошибка: EMBEDDING_API_URL не задан в .env")
        return

    try:
        text = "Проверка связи с Hugging Face Space и моделью BGE-M3"
        dense, sparse = client.embed_text(text)
        
        print(f"\n✅ Успешный ответ от сервера!")
        print(f"Размерность Dense-вектора: {len(dense)} (ожидалось 1024)")
        
        if len(dense) == 1024:
            print("✨ Размерность верная для BGE-M3")
        else:
            print(f"⚠️ Внимание: Размерность {len(dense)} отличается от ожидаемой 1024")
            
        if sparse:
            print(f"✅ Sparse-вектор получен! Количество индексов: {len(sparse.indices)}")
        else:
            print("❌ Sparse-вектор НЕ получен (проверьте настройки Infinity)")
            
    except Exception as e:
        print(f"\n❌ Ошибка при запросе: {e}")

if __name__ == "__main__":
    test_hf_integration()
