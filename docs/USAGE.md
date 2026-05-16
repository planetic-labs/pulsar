# Usage Guide

## 🔑 Authentication
The system uses a simple token-based authentication. Set your `APP_ACCESS_TOKEN` in the `.env` file. You will be prompted to enter this token when you first visit the application.

---

## 📥 Importing Videos
To start indexing your content:
1.  Navigate to the **Import** page.
2.  Browse your Google Drive hierarchy.
3.  Click the **"Import"** button next to a file or a folder.
4.  The system will:
    - Check for duplicates (using MD5 hash).
    - Queue the file for download and processing.

---

## 🔍 Searching
Pulsar AI provides several search modes accessible via the "Mode" dropdown:

- **Hybrid (Recommended)**: Combines semantic meaning and keywords for the best results.
- **Semantic**: Finds content based on meaning, even if exact words aren't present.
- **Lexical**: Traditional keyword-based search.
- **Quotes**: Precision phrase matching using high-resolution timestamps from Deepgram utterances.

### Filtering
- **Date Range**: Use the dual-slider timeline to restrict results to a specific period.
- **Content Type**: Switch between "All", "Shorts" (under 30 mins), and "Long" videos.
- **Speaker**: Filter results by a specific speaker (if names have been assigned).

---

## 👥 Speaker Management
The system automatically detects different voices (diarization).
1.  Open any video from the search results.
2.  Click the **"Speakers"** icon.
3.  Assign human names to the detected tags (e.g., `Speaker 0` -> `John Doe`).
4.  These names will be used for filtering in future searches.

---

## 📊 Monitoring the Pipeline
The **Status** page provides real-time insights into the processing queue:
- **Download Card**: Shows current download speed and file progress.
- **Transcription Card**: Shows the upload speed to Deepgram and current status.
- **Indexing Card**: Shows embedding generation progress.
- **Error Log**: If a task fails, the error message will appear here. You can click "Restart All" to retry failed tasks.

---

## 🛠 Maintenance Tasks

### Full Reindex
If you change your embedding model or update the indexing logic, you can trigger a full reindex:
`POST /api/v1/reindex/all?clear_qdrant=true`
This will clear the Qdrant collection and re-process all transcriptions from SQLite.

### Integrity Check
Run this script inside the container to ensure SQLite and Qdrant are in sync:
```bash
docker compose exec app uv run scripts/check_integrity.py
```
