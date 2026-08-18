# 🎬 Pulsar

Enterprise-grade RAG (Retrieval-Augmented Generation) system for deep semantic search through video archives using AI transcription. Optimized for high-speed processing and precise quote retrieval.

[Russian Description / Описание на русском](#pulsar-ru)

---

## 🚀 Key Features

- **Hybrid Search (RRF)**: Combines Dense vectors (BGE-M3), Sparse vectors, and SQL Quote Boost for maximum precision.
- **3-Stage Pipeline**: Parallel processing (Download -> Transcribe -> Index) with automatic lifecycle management.
- **Smart Transcription**: Powered by Deepgram Nova-3 with diarization and automated post-processing.
- **Google Drive Native**: Seamless integration via Service Account with support for Shared Drives and large folder pagination.
- **MD5 Deduplication**: Prevents redundant processing by tracking file content hashes.
- **Tiered Caching**: Two-layer embedding cache (L1 Memory + L2 SQLite) for sub-millisecond response times.
- **Mobile-First & PWA**: Dedicated mobile UI and Progressive Web App support for installation on iOS/Android.

## 🛠 Tech Stack

- **Backend**: FastAPI (Python 3.14+)
- **Storage**: SQLite (Metadata & Cache) + Manticore Search (Vector Search)
- **AI Integrations**: Infinity Remote (Embeddings), Deepgram API (ASR)
- **Infrastructure**: Docker & Docker Compose
- **Package Manager**: `uv`

## 🏁 Quick Start

1.  **Setup Environment**:
    ```bash
    cp .env.example .env
    # Edit .env and fill in APP_ACCESS_TOKEN, DEEPGRAM_API_KEY, and EMBEDDING_API_URL
    ```
2.  **Configure Google Drive**:
    Place your Service Account JSON key in `config/service-key.json`.
3.  **Launch**:
    ```bash
    docker compose up -d --build
    ```

The application will be available at `http://localhost:8351`.

## 📂 Documentation

Detailed documentation is available in the `docs/` directory:
- [**Installation Guide**](./docs/INSTALLATION.md) - Full environment setup and Google Cloud configuration.
- [**Architecture Overview**](./docs/ARCHITECTURE.md) - Pipeline design and data flow.
- [**Usage Guide**](./docs/USAGE.md) - Importing content, searching, and managing speakers.

## 🔧 Maintenance & CLI

Administrative utilities accessible via Docker:
- **Clean Queue**: `docker compose exec app uv run python scripts/clear_queue.py`
- **Integrity Check**: `docker compose exec app uv run scripts/verify_integrity.py`
- **Full Reindex**: `docker compose exec app uv run scripts/reindex_search.py`
- **Backup**: `./cron/restic_backup.sh`

## 🛡 Security

- Authorization via access token (`APP_ACCESS_TOKEN`).
- Sensitive configuration is isolated in the `config/` directory.
- Deepgram Balance Protection: Worker automatically halts if the account balance is low (< $1.00).

---

## Pulsar (RU)

Корпоративная RAG-система для глубокого семантического поиска по архиву видео с использованием AI-транскрибации. Система оптимизирована для работы с русскоязычным контентом и обеспечивает мгновенный поиск по смыслу и точным цитатам.

### Основные возможности
- Гибридный поиск (Dense + Sparse + SQL).
- Трехстадийный параллельный конвейер обработки.
- Интеграция с Google Drive через Service Account.
- Автоматическая дедупликация по MD5 и кеширование эмбеддингов.

## 📜 License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
