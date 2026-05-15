# 🎬 VideoDB AI

<p align="left">
  <a href="https://github.com/arassypnov/search-ui/actions/workflows/docker-publish.yml">
    <img src="https://github.com/arassypnov/search-ui/actions/workflows/docker-publish.yml/badge.svg" alt="Build Status">
  </a>
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python Version">
  <a href="https://github.com/astral-sh/uv">
    <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="Built with uv">
  </a>
  <a href="https://github.com/astral-sh/ruff">
    <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff">
  </a>
  <img src="https://img.shields.io/badge/types-Ty-blue" alt="Checked with Ty">
  <a href="https://github.com/pre-commit/pre-commit">
    <img src="https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit" alt="Pre-commit">
  </a>
  <a href="https://deepgram.com/">
    <img src="https://img.shields.io/badge/STT-Deepgram-black?logo=deepgram" alt="Deepgram">
  </a>
  <a href="https://qdrant.tech/">
    <img src="https://img.shields.io/badge/VectorDB-Qdrant-red?logo=qdrant" alt="Qdrant">
  </a>
  <img src="https://img.shields.io/badge/PWA-ready-orange.svg" alt="PWA">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT">
  </a>
</p>

Enterprise-grade RAG (Retrieval-Augmented Generation) system for deep semantic search through video archives using AI transcription. Optimized for high-speed processing and precise quote retrieval.

[Russian Description / Описание на русском](#видео-бд-ai)

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

- **Backend**: FastAPI (Python 3.12+)
- **Storage**: SQLite (Metadata & Cache) + Qdrant (Vector Search)
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

The application will be available at `http://localhost:8000`.

## 📂 Documentation

Detailed documentation is available in the `docs/` directory:
- [**Installation Guide**](./docs/INSTALLATION.md) - Full environment setup and Google Cloud configuration.
- [**Architecture Overview**](./docs/ARCHITECTURE.md) - Pipeline design and data flow.
- [**Usage Guide**](./docs/USAGE.md) - Importing content, searching, and managing speakers.

## 🔧 Maintenance & CLI

Administrative utilities accessible via Docker:
- **Clean Queue**: `docker compose exec app uv run python scripts/clear_queue.py`
- **Integrity Check**: `docker compose exec app uv run scripts/check_integrity.py`
- **Full Reindex**: `docker compose exec app uv run scripts/reindex_search.py`
- **Backup**: `cd backups && uv run backup.py`

## 🛡 Security

- Authorization via access token (`APP_ACCESS_TOKEN`).
- Sensitive configuration is isolated in the `config/` directory.
- Deepgram Balance Protection: Worker automatically halts if the account balance is low (< $1.00).

---

## Видео-БД AI

Корпоративная RAG-система для глубокого семантического поиска по архиву видео с использованием AI-транскрибации. Система оптимизирована для работы с русскоязычным контентом и обеспечивает мгновенный поиск по смыслу и точным цитатам.

### Основные возможности
- Гибридный поиск (Dense + Sparse + SQL).
- Трехстадийный параллельный конвейер обработки.
- Интеграция с Google Drive через Service Account.
- Автоматическая дедупликация по MD5 и кеширование эмбеддингов.

## 📜 License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
