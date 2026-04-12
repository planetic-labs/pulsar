# search-ui

MVP сервиса поиска по русскоязычным видео из Google Drive.

## Что уже умеет проект

- авторизация в Google Drive и скачивание видео
- извлечение аудио из `mp4`
- транскрибация через Deepgram
- хранение `videos`, `transcripts`, `chunks` в SQLite
- keyword search + Gemini embeddings semantic search
- JSON API
- web UI поиска, статусов и локального плеера с переходом по таймкоду

## Стек

- `uv`
- `FastAPI`
- `SQLite`
- `imageio-ffmpeg`
- `Deepgram API`
- `Gemini embeddings API`

## Подготовка

1. Синхронизировать окружение:

```bash
/home/devman/.local/bin/uv sync
```

2. Проверить локальный `.env` на основе `.env.example`

Основные параметры:

- `APP_ACCESS_TOKEN` — токен доступа к UI и API
- `DEEPGRAM_API_KEY` — ключ для транскрибации
- `GEMINI_API_KEY` — ключ для embeddings
- `GOOGLE_DRIVE_CREDENTIALS_PATH` — путь до `google.json`

## Google Drive OAuth

1. Сгенерировать ссылку авторизации:

```bash
/home/devman/.local/bin/uv run python scripts/drive_cli.py auth-init
```

2. Открыть ссылку в браузере и после логина скопировать полный callback URL вида `http://localhost/?...`

3. Обменять callback URL на токен:

```bash
/home/devman/.local/bin/uv run python scripts/drive_cli.py auth-exchange '<FULL_CALLBACK_URL>'
```

4. Посмотреть доступные файлы:

```bash
/home/devman/.local/bin/uv run python scripts/drive_cli.py list --page-size 10
```

## Быстрый ingest одного файла

Полный оркестрационный путь:

```bash
/home/devman/.local/bin/uv run python scripts/ingest_drive_file.py <FILE_ID> --clip-duration-sec 60
```

Это делает:

- скачивание видео из Google Drive
- извлечение WAV
- транскрибацию через Deepgram
- сохранение в SQLite
- пересборку поискового индекса

## Ручной пайплайн

Скачать файл:

```bash
/home/devman/.local/bin/uv run python scripts/drive_cli.py download <FILE_ID> --output downloads/<filename>.mp4
```

Извлечь короткий клип:

```bash
/home/devman/.local/bin/uv run python scripts/extract_audio.py downloads/<filename>.mp4 \
  --output audio/<name>-clip.wav \
  --start-sec 0 \
  --duration-sec 60
```

Отправить в Deepgram:

```bash
/home/devman/.local/bin/uv run python scripts/transcribe_deepgram.py audio/<name>-clip.wav
```

Зарегистрировать результат в базе:

```bash
/home/devman/.local/bin/uv run python scripts/register_transcript.py \
  --source-file-id <GOOGLE_FILE_ID> \
  --title '<VIDEO_TITLE>' \
  --source-url 'https://drive.google.com/file/d/<GOOGLE_FILE_ID>/view' \
  --size-bytes <SIZE_BYTES> \
  --duration-sec <DURATION_SEC> \
  --video-path downloads/<filename>.mp4 \
  --audio-path audio/<name>-clip.wav \
  --raw-transcript-path transcripts/<name>.deepgram.json \
  --normalized-transcript-path transcripts/<name>.normalized.json \
  --summary-output data/<name>.summary.json
```

Пересобрать индекс вручную:

```bash
/home/devman/.local/bin/uv run python scripts/reindex_search.py
```

CLI-поиск:

```bash
/home/devman/.local/bin/uv run python scripts/search_cli.py 'как слышно меня'
```

## Запуск MVP-сервера

```bash
/home/devman/.local/bin/uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Docker

Сервис можно запустить в Docker, при этом папка `data` остаётся на хосте.

Сборка и запуск:

```bash
docker compose up -d --build
```

Остановка:

```bash
docker compose down
```

Важно:

- база и индекс хранятся в `./data` на хосте через bind mount
- `google.json`, `token.json`, `token.auth.json` подмонтируются в контейнер

## Основные маршруты

- `GET /health`
- `GET /api/search?q=...&token=...`
- `GET /api/status?token=...`
- `GET /?q=...&token=...`
- `GET /status?token=...`
- `GET /videos/<video_id>?token=...&start=<seconds>`

## Важно

- Текущий [google.json](/srv/search-ui/google.json) — это `OAuth client`, а не `service account`
- Для MVP-хранилища используется SQLite в [app.db](/srv/search-ui/data/app.db)
- Semantic search сейчас строится на Gemini embeddings через `gemini-embedding-001`
- Для production-масштаба позже можно вынести векторное хранение в PostgreSQL `pgvector` или отдельный vector DB
