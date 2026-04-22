# План миграции на Hugging Face Inference Endpoints (Voice API)

## Цель
Вынести тяжелую модель распознавания спикеров (SpeechBrain ECAPA-TDNN) с локального CPU на выделенный API-эндпоинт. Это полностью устранит падения контейнера из-за нехватки памяти (OOM) и ускорит индексацию.

## Шаг 1: Подготовка на стороне Hugging Face
1.  **Выбор модели**: `speechbrain/spkrec-ecapa-voxceleb`.
2.  **Создание Endpoint**: 
    *   Тип: Protected (нужен API Token).
    *   Инстанс: CPU (small) или GPU (T4). Для 192-мерных векторов CPU достаточно.
    *   Task: "Audio Classification" или "Custom" (через `handler.py` для возврата векторов).
3.  **Получение данных**: Сохранить `HF_TOKEN` и `HF_VOICE_URL`.

## Шаг 2: Изменение Backend (VideoDB)
1.  **app/config.py**:
    *   Добавить `HF_TOKEN` и `HF_VOICE_URL` в настройки.
2.  **app/voice.py**:
    *   Удалить импорты `torch`, `speechbrain`, `gc`.
    *   Переписать `extract_speaker_embedding` на использование `httpx.post`.
    *   Логика: нарезаем аудио через `ffmpeg` (как сейчас) -> отправляем байты в HF -> получаем JSON с вектором -> возвращаем список float.
3.  **app/worker.py**:
    *   Удалить принудительную очистку памяти (станет не нужна).

## Шаг 3: Оптимизация зависимостей
1.  **pyproject.toml**:
    *   Удалить `torch`, `torchaudio`, `speechbrain`. Это уменьшит размер Docker-образа на 2-3 ГБ.
2.  **Dockerfile**:
    *   Удалить этапы, связанные с установкой тяжелых ML-библиотек.

## Шаг 4: Тестирование
1.  Запуск `scripts/ingest_drive_file.py` на тестовом видео.
2.  Проверка логов: "Auto-recognized speaker..." должен работать через API.
3.  Мониторинг потребления RAM (ожидаемое снижение: с 4-5 ГБ до < 1 ГБ).

---
**Текущий статус**: План утвержден, реализация начнется завтра.
