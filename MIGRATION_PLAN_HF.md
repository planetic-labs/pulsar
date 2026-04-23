# План миграции на Hugging Face (Cloud Hosting)

## Цель
Перенести инференс тяжелых моделей (текстовые и голосовые эмбеддинги) на **Hugging Face Inference Endpoints** или **Spaces**. Это обеспечит высокую скорость обработки (с использованием GPU на стороне HF) и снизит нагрузку на локальный сервер.

## Архитектура ☁️

### 1. Текстовые эмбеддинги (Search & RAG)
- **Модель**: `BAAI/bge-m3` (Dense 1024d + Sparse).
- **Сервис**: `Infinity` (папка `hf_embed_service`).
- **Деплой**: Hugging Face Spaces (Docker SDK) или Inference Endpoints.
- **Порт**: 7860.
- **URL**: `EMBEDDING_API_URL` (например, `https://username-space.hf.space`).

### 2. Голосовые эмбеддинги (Speaker ID)
- **Модель**: `SpeechBrain ECAPA-TDNN`.
- **Сервис**: Кастомный `handler.py` (папка `hf_endpoint`).
- **Деплой**: Hugging Face Inference Endpoints (Custom Handler).
- **URL**: `VOICE_API_URL`.

## Выполненные шаги ✅
1.  **Текстовый сервис**: Создан Dockerfile для Infinity (порт 7860).
2.  **Голосовой сервис**: Создан `handler.py` для HF Inference Endpoints.
3.  **Клиент**: `UnifiedEmbeddingClient` в `app/gemini.py` готов к работе с удаленным API.
4.  **Скрипт**: `reindex_search.py` адаптирован для новых форматов векторов.

## Предстоящие шаги 🚀

### 1. Деплой на Hugging Face
- Создать новый Space (Docker) для `hf_embed_service`.
- Создать Inference Endpoint для `hf_endpoint` (указав репозиторий с `handler.py`).

### 2. Настройка локального приложения
В файле `.env` прописать полученные URL:
```bash
EMBEDDING_API_URL=https://your-space-url.hf.space
EMBEDDING_API_TOKEN=hf_your_token
VOICE_API_URL=https://your-voice-endpoint.hf.endpoint.url
VOICE_API_TOKEN=hf_your_token
```

### 3. Полная переиндексация (Semantic)
Так как мы переходим с Gemini (768d) на BGE-M3 (1024d), нужно сбросить индекс:
```bash
python scripts/reindex_search.py --full
```

### 4. Пересчет спикеров (Voice)
Если мы меняем модель Voice ID на SpeechBrain, старые эмбеддинги в `speaker_registry` (Qdrant) тоже нужно обновить.
```bash
# Скрипт для массового обновления спикеров (потребует запуска Voice API)
python scripts/reprocess_speakers.py
```

## Риски и нюансы
- **Безопасность**: Обязательно использовать `EMBEDDING_API_TOKEN` для защиты публичных эндпоинтов.
- **Latency**: Время ответа будет зависеть от сетевого пинга до HF.
