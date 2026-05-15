# Architecture Overview

VideoDB AI is built as a highly modular RAG (Retrieval-Augmented Generation) system optimized for video content. It leverages a modern asynchronous stack to handle heavy processing tasks like audio extraction, transcription, and vector indexing.

## Core Components

### 1. Web API (FastAPI)
The central hub of the application. It handles:
- User authentication and session management.
- Google Drive file browsing and ingestion triggers.
- Hybrid search execution (Dense + Sparse).
- Real-time progress tracking and log streaming via WebSockets.

### 2. Multi-Stage Worker Pipeline
The worker operates as a three-stage independent consumer system using SQLite as a task queue:
- **Stage 1 (Download)**: Fetches video from Google Drive, extracts the audio track using FFmpeg, and cleans up the video file immediately to save disk space.
- **Stage 2 (Transcription)**: Uploads audio to Deepgram (Nova-3 model) with diarization. Results are saved as raw and normalized JSON files.
- **Stage 3 (Indexing)**: Generates embeddings for text chunks using the Infinity Remote Service and upserts them into Qdrant.

### 3. Data Storage (Hybrid Model)
- **SQLite**: Stores structured metadata, folder hierarchy, speaker names, task queue, and persistent embedding cache. It uses WAL mode for better concurrency.
- **Qdrant**: A high-performance vector database used for semantic and lexical search. Each point in Qdrant corresponds to a text chunk in SQLite.

## Search Strategy (Hybrid RRF)

VideoDB uses **Reciprocal Rank Fusion (RRF)** to combine results from multiple search methods:
1.  **Dense Retrieval**: Uses BGE-M3 embeddings for semantic "meaning-based" search.
2.  **Sparse Retrieval**: Uses sparse vectors for precise "keyword-based" search.
3.  **SQL Quote Boost**: Uses SQLite `REGEXP` for exact phrase matching and "Quote Search" mode.

## System Workflow

1.  **Ingestion**: User selects files in the UI. Metadata is saved to SQLite, and a `stage_1` task is created.
2.  **Processing**: The background worker picks up tasks sequentially. Progress is broadcasted to all connected UI clients.
3.  **Search**: When a user queries, the system fetches embeddings (or pulls from cache), queries Qdrant, hydrates results with fresh metadata from SQLite, and applies highlighting.

## Directory Structure

- `app/`: Core application logic (API, worker, search).
- `scripts/`: Maintenance and administrative CLI tools.
- `templates/`: Jinja2 templates for the web interface.
- `static/`: Frontend assets (icons, styles).
- `backups/`: Independent S3 backup plugin.
- `config/`: Configuration files and Service Account keys.
