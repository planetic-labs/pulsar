# База данных Pulsar (Версия 1)

Этот файл содержит актуальную структуру базы данных SQLite (`data/pulsar.db`), описание таблиц, ограничений, индексов и триггеров для быстрого контекста модели.

Код инициализации БД находится в [app/db.py](file:///home/devman/workspace/pulsar-qwin-embeding/app/db.py).

---

## 1. Структура таблиц

### 1.1. `folders` (Иерархия директорий)
Хранит структуру папок для организации видеофайлов.
```sql
CREATE TABLE folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id TEXT REFERENCES folders(id) ON DELETE CASCADE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    CHECK (parent_id IS NULL OR parent_id != id)
);
```

### 1.2. `videos` (Записи видеофайлов)
Основная таблица видео. Поддерживает связь с родительской папкой и ссылается на оригинал в случае дубликата.
```sql
CREATE TABLE videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id TEXT NOT NULL,
    parent_folder_id TEXT REFERENCES folders(id) ON DELETE SET NULL,
    md5_checksum TEXT,
    title TEXT NOT NULL,
    recorded_date DATE,
    is_short BOOLEAN DEFAULT FALSE,
    source_url TEXT,
    mime_type TEXT,
    size_bytes BIGINT,
    duration_sec DOUBLE PRECISION,
    status TEXT NOT NULL,
    is_4k BOOLEAN DEFAULT FALSE,
    is_missing BOOLEAN DEFAULT FALSE,
    is_excluded BOOLEAN DEFAULT FALSE,
    original_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_file_id),
    CHECK (original_id IS NULL OR original_id != id),
    CHECK (size_bytes IS NULL OR size_bytes >= 0),
    CHECK (duration_sec IS NULL OR duration_sec >= 0)
);
```
* **Оригинал видео**: `original_id IS NULL`.
* **Дубликат видео**: `original_id IS NOT NULL` (ссылается на `id` оригинального видео).

### 1.3. `chunks` (Сегменты транскрипта)
Текст транскрипции, разбитый на смысловые временные интервалы.
```sql
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    start_sec DOUBLE PRECISION NOT NULL,
    end_sec DOUBLE PRECISION NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(video_id, chunk_index),
    CHECK(chunk_index >= 0),
    CHECK(start_sec >= 0),
    CHECK(end_sec >= start_sec),
    CHECK(length(trim(text)) > 0)
);
```

### 1.4. `tasks` (Очередь фоновых задач воркера)
Асинхронная очередь обработки видео.
```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    error_message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 1.5. `query_cache` (Кэш эмбеддингов для поисковых запросов)
```sql
CREATE TABLE query_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT UNIQUE NOT NULL,
    dense_vector BLOB NOT NULL,
    sparse_indices BLOB,
    sparse_values BLOB,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 1.6. `revoked_sessions` / `revoked_users` (Черные списки JWT-сессий и пользователей)
```sql
CREATE TABLE revoked_sessions (
    jti TEXT PRIMARY KEY,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE revoked_users (
    user_id TEXT PRIMARY KEY,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 2. Индексы

* `idx_videos_parent_folder` на `videos(parent_folder_id)` — быстрый поиск файлов в папке.
* `idx_videos_md5` на `videos(md5_checksum)` — поиск дубликатов по хэшу.
* `idx_query_cache_query` на `query_cache(query)` — быстрый lookup кэша.
* `idx_videos_original_id` на `videos(original_id)` — связь дубликата с оригиналом.
* `idx_videos_status` на `videos(status)` — выборка по статусу обработки.
* `idx_folders_parent_name` на `folders(parent_id, name)` — сортировка папок при выводе.
* `idx_tasks_queue` на `tasks(status, priority DESC, created_at ASC)` — приоритетная выборка задач воркером.
* `uidx_videos_md5_original` (уникальный частичный) на `videos(md5_checksum) WHERE original_id IS NULL AND md5_checksum IS NOT NULL AND md5_checksum != ''` — гарантирует, что на один MD5 может существовать только один оригинал.

---

## 3. Триггеры (Бизнес-логика на уровне БД)

### 3.1. Автообновление даты изменения (`updated_at`)
* `trg_videos_updated_at`: После обновления строки в `videos` устанавливает `updated_at = CURRENT_TIMESTAMP`.
* `trg_tasks_updated_at`: После обновления строки в `tasks` устанавливает `updated_at = CURRENT_TIMESTAMP`.

### 3.2. Защита от циклов и глубоких цепочек дубликатов
* `trg_videos_prevent_duplicate_chains_insert` и `trg_videos_prevent_duplicate_chains_update`: Блокируют операцию, если поле `original_id` указывает на видео, которое само является дубликатом (запрет цепочек вида A -> B -> C).
* `trg_folders_prevent_loops`: Рекурсивный триггер для `folders`. Блокирует перенос папки, если целевая родительская папка является её собственным потомком (защита от бесконечного цикла вложенности).

---

## 4. Хранение транскриптов
Таблица `transcripts` удалена. Файлы транскрипции хранятся физически на диске в сжатом и шардированном виде для повышения производительности файловой системы и экономии места:

* **Raw Deepgram JSON**: `storage/transcripts/raw/{prefix}/{source_file_id}.json.gz`
* **Normalized JSON**: `storage/transcripts/normalized/{prefix}/{source_file_id}.json.gz`

### 4.1. Правила организации хранения
1. **Шардирование**: `{prefix}` представляет собой первые 2 символа от `source_file_id`. Если `source_file_id` короче 2 символов, то используется сам ID. Это предотвращает накопление критического числа файлов в одной директории.
2. **Именование**: Имена файлов строго соответствуют формату `{source_file_id}.json.gz`. Все метки моделей (например, `dg_nova3_`) и движков (например, `_deepgram`) из названия файлов удалены.
3. **Формат и сжатие**:
   - Данные упакованы с использованием сжатия `gzip` (`.json.gz`).
   - JSON-содержимое очищено от отступов и пробелов форматирования (компактный JSON, `indent=None`, separators `(',', ':')`) для минимизации размера.
4. **Разрешение дубликатов**: При импорте или миграции в случае обнаружения дублирующихся файлов для одного `source_file_id` приоритет отдается самому свежему файлу на основе времени изменения файла (`mtime`).
5. **Жизненный цикл**:
   - Вся логика обращается к файлам напрямую по `source_file_id` оригинального видео.
   - При обмене ролями оригинал/дубликат файлы на диске переименовываются под новый `source_file_id` и переносятся в соответствующую директорию шардирования.

