import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db import db_connection
from scripts.sync_index import main as sync_main
from scripts.sync_index import scan_folder_recursive, send_telegram_duplicate_alerts, send_telegram_notification


@pytest.mark.asyncio
async def test_send_telegram_notification(monkeypatch, mocker):
    monkeypatch.setattr("scripts.sync_index.TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setattr("scripts.sync_index.TELEGRAM_CHAT_ID", "7890")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    missing_files = [
        {"file_id": "file_1", "title": "Missing Video 1", "source_url": "https://drive.google.com/1"},
        {"file_id": "file_2", "title": "Missing Video 2 (4K)", "source_url": "https://drive.google.com/2"},
    ]

    await send_telegram_notification(missing_files)

    assert mock_post.called
    args, kwargs = mock_post.call_args
    url = args[0]
    assert "123456:ABC" in url

    payload = kwargs["json"]
    assert payload["chat_id"] == "7890"
    assert "Missing Video 1" in payload["text"]
    assert "Missing Video 2" in payload["text"]


@pytest.mark.asyncio
async def test_send_telegram_duplicate_alerts(monkeypatch, mocker):
    monkeypatch.setattr("scripts.sync_index.TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setattr("scripts.sync_index.TELEGRAM_CHAT_ID", "7890")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    duplicates = [{"title": "Duplicate Video", "new_file_id": "new_fid", "existing_file_id": "existing_fid"}]

    await send_telegram_duplicate_alerts(duplicates)

    assert mock_post.called
    args, kwargs = mock_post.call_args
    payload = kwargs["json"]
    assert "Duplicate Video" in payload["text"]
    assert "new_fid" in payload["text"]
    assert "existing_fid" in payload["text"]


@pytest.mark.asyncio
async def test_scan_folder_recursive():
    drive_mock = MagicMock()

    async def mock_list(folder_id, use_cache=False):
        if folder_id == "root_id":
            return [
                {"id": "subfolder_id", "name": "Subfolder", "is_folder": True},
                {
                    "id": "video_id",
                    "name": "Video.mp4",
                    "is_folder": False,
                    "mime_type": "video/mp4",
                    "md5_checksum": "abc",
                },
                {"id": "excluded_id", "name": "ГАЛЕРЕЯ СЕМЬИ.mp4", "is_folder": False, "mime_type": "video/mp4"},
                {"id": "text_id", "name": "Notes.txt", "is_folder": False, "mime_type": "text/plain"},
            ]
        return []

    drive_mock.list_folder_contents = mock_list

    visited_folders = []
    drive_files = []
    excluded_by_keyword_ids = []

    await scan_folder_recursive(
        drive=drive_mock,
        folder_id="root_id",
        parent_id=None,
        folder_name="Root",
        visited_folders=visited_folders,
        drive_files=drive_files,
        exclude_keywords=("ГАЛЕРЕЯ",),
        excluded_by_keyword_ids=excluded_by_keyword_ids,
    )

    assert {"id": "root_id", "name": "Root", "parent_id": None} in visited_folders
    assert {"id": "subfolder_id", "name": "Subfolder", "parent_id": "root_id"} in visited_folders

    # "Video.mp4" must be added, but "ГАЛЕРЕЯ СЕМЬИ.mp4" must be excluded
    file_ids = {f["file_id"] for f in drive_files}
    assert "video_id" in file_ids
    assert "excluded_id" not in file_ids
    assert "excluded_id" in excluded_by_keyword_ids
    assert len(drive_files) == 1


@pytest.mark.asyncio
async def test_sync_main_flow(tmp_db, monkeypatch, mocker):
    # Setup test env settings and database
    monkeypatch.setattr("scripts.sync_index.get_sqlite_settings", lambda: tmp_db)

    # Mock AppSettings with exclude_keywords
    mock_settings = MagicMock()
    mock_settings.port = 8000
    mock_settings.access_token = "test-token"
    mock_settings.exclude_keywords = ("ГАЛЕРЕЯ",)
    monkeypatch.setattr("scripts.sync_index.get_app_settings", lambda: mock_settings)

    # Pre-populate internal database
    with db_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO folders (id, name, parent_id) VALUES (?, ?, ?)", ("root_folder_id", "Indexed Root", None)
        )
        conn.execute(
            "INSERT INTO folders (id, name, parent_id) VALUES (?, ?, ?)",
            ("old_folder_to_delete", "Old Folder", "root_folder_id"),
        )

        # Existing video that is updated (title changes, recorded_date changes, is_4k changes)
        conn.execute(
            """
            INSERT INTO videos (
                id, source_file_id, title, parent_folder_id,
                recorded_date, is_4k, processing_status, source_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                101,
                "video_to_update",
                "2020.01.01 Old Title.mp4",
                "root_folder_id",
                "2020-01-01",
                0,
                "transcribed",
                "google_drive",
            ),
        )
        # Existing video that was removed
        conn.execute(
            """
            INSERT INTO videos (
                id, source_file_id, title, parent_folder_id,
                processing_status, source_type, source_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                102,
                "video_removed",
                "2020.02.02 Removed Video.mp4",
                "root_folder_id",
                "indexed_chunks_ready",
                "google_drive",
                "https://drive.google.com/removed",
            ),
        )
        # Existing video that remains unchanged (and will match a duplicate check for a new file)
        conn.execute(
            """
            INSERT INTO videos (
                id, source_file_id, title, parent_folder_id,
                recorded_date, is_4k, processing_status, source_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                103,
                "video_unchanged",
                "2026.05.01 Unchanged Video.mp4",
                "root_folder_id",
                "2026-05-01",
                0,
                "transcribed",
                "google_drive",
            ),
        )

    # Mock GoogleDriveClient
    drive_mock = MagicMock()

    # Root folder contents:
    # - New subfolder: "new_subfolder_id"
    # - Updated video: "video_to_update" (new title: "2026.05.20 New Title 4K.mp4")
    # - Unchanged video: "video_unchanged"
    # - Excluded video: "excluded_video" ("2026.05.01 ГАЛЕРЕЯ.mp4")
    # - New video duplicate: "new_video_duplicate" ("2026.05.01 Unchanged Video.mp4")
    # - New video to queue: "new_video_id" (title: "Brand New Video.mp4")
    async def mock_list_contents(folder_id, use_cache=False):
        if folder_id == "root":
            return [{"id": "root_folder_id", "name": "Indexed Root", "is_folder": True}]
        elif folder_id == "root_folder_id":
            return [
                {"id": "new_subfolder_id", "name": "New Subfolder", "is_folder": True},
                {
                    "id": "video_to_update",
                    "name": "2026.05.20 New Title 4K.mp4",
                    "is_folder": False,
                    "mime_type": "video/mp4",
                },
                {
                    "id": "video_unchanged",
                    "name": "2026.05.01 Unchanged Video.mp4",
                    "is_folder": False,
                    "mime_type": "video/mp4",
                },
                {
                    "id": "excluded_video",
                    "name": "2026.05.01 ГАЛЕРЕЯ.mp4",
                    "is_folder": False,
                    "mime_type": "video/mp4",
                },
                {
                    "id": "new_video_duplicate",
                    "name": "2026.05.01 Unchanged Video.mp4",
                    "is_folder": False,
                    "mime_type": "video/mp4",
                },
                {"id": "new_video_id", "name": "Brand New Video.mp4", "is_folder": False, "mime_type": "video/mp4"},
            ]
        elif folder_id == "new_subfolder_id":
            return []
        return []

    drive_mock.list_folder_contents = mock_list_contents
    mocker.patch("scripts.sync_index.GoogleDriveClient", return_value=drive_mock)

    # Mock Telegram notifications
    mock_telegram = mocker.patch("scripts.sync_index.send_telegram_notification", new_callable=AsyncMock)
    mock_duplicate = mocker.patch("scripts.sync_index.send_telegram_duplicate_alerts", new_callable=AsyncMock)

    # Mock HTTP client responses to test worker activation
    # GET /api/v1/worker/status -> {"is_running": False}
    # POST /api/v1/worker/start -> {"status": "starting"}
    mock_response_status = MagicMock()
    mock_response_status.status_code = 200
    mock_response_status.json.return_value = {"is_running": False}

    mock_response_start = MagicMock()
    mock_response_start.status_code = 200
    mock_response_start.json.return_value = {"status": "starting"}

    async def mock_request(self, method, url, *args, **kwargs):
        if method == "GET" and "worker/status" in url:
            return mock_response_status
        elif method == "POST" and "worker/start" in url:
            return mock_response_start
        raise ValueError(f"Unexpected request {method} {url}")

    mocker.patch("httpx.AsyncClient.request", mock_request)

    # Run main sync logic
    await sync_main()

    # Assertions
    with db_connection(tmp_db) as conn:
        # 1. Folders:
        # - "old_folder_to_delete" must be deleted
        folders = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM folders").fetchall()}
        assert "old_folder_to_delete" not in folders

        # 2. Updated video metadata checks:
        updated_video = conn.execute(
            "SELECT title, recorded_date, is_4k FROM videos WHERE source_file_id = 'video_to_update'"
        ).fetchone()
        assert updated_video["title"] == "2026.05.20 New Title 4K.mp4"

        # 3. New video checks:
        # - "excluded_video" must not be queued because it contains 'ГАЛЕРЕЯ'
        tasks = conn.execute("SELECT task_type, payload FROM tasks").fetchall()
        download_tasks = [t for t in tasks if t["task_type"] == "stage_1_download"]
        queued_titles = [json.loads(t["payload"])["title"] for t in download_tasks]

        assert "Brand New Video.mp4" in queued_titles
        assert "2026.05.01 ГАЛЕРЕЯ.mp4" not in queued_titles

    # 4. Removed video checks:
    mock_telegram.assert_called_once()

    # 5. Duplicate title alert checks:
    # - Should detect that "new_video_duplicate" has same name as "video_unchanged" in DB
    mock_duplicate.assert_called_once()
    dup_args = mock_duplicate.call_args[0][0]
    assert len(dup_args) == 1
    assert dup_args[0]["title"] == "2026.05.01 Unchanged Video.mp4"
    assert dup_args[0]["new_file_id"] == "new_video_duplicate"
    assert dup_args[0]["existing_file_id"] == "video_unchanged"


@pytest.mark.asyncio
async def test_sync_main_flow_gdrive_error(tmp_db, monkeypatch, mocker):
    mock_settings = MagicMock()
    mock_settings.db_path = tmp_db
    mock_settings.port = 8000
    mock_settings.access_token = "test-token"
    mock_settings.exclude_keywords = ("ГАЛЕРЕЯ",)
    monkeypatch.setattr("scripts.sync_index.get_app_settings", lambda: mock_settings)

    # Pre-populate database with a folder and a video
    with db_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO folders (id, name, parent_id) VALUES (?, ?, ?)", ("root_folder_id", "Indexed Root", None)
        )
        conn.execute(
            """
            INSERT INTO videos (
                id, source_file_id, title, parent_folder_id,
                recorded_date, is_4k, processing_status, source_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                103,
                "video_unchanged",
                "2026.05.01 Unchanged Video.mp4",
                "root_folder_id",
                "2026-05-01",
                0,
                "transcribed",
                "google_drive",
            ),
        )

    # Mock GoogleDriveClient to throw exception
    drive_mock = MagicMock()

    async def mock_list_contents_error(folder_id, use_cache=False):
        raise RuntimeError("API Connection Error")

    drive_mock.list_folder_contents = mock_list_contents_error
    mocker.patch("scripts.sync_index.GoogleDriveClient", return_value=drive_mock)

    # Mock Telegram alert call
    mock_telegram_text = mocker.patch("scripts.sync_index.send_telegram_text", new_callable=AsyncMock)

    # Run main sync logic
    await sync_main()

    # Verify that synchronization was aborted, Telegram notification was sent,
    # and the database remained unmodified (no folders or videos deleted).
    mock_telegram_text.assert_called_once()
    alert_text = mock_telegram_text.call_args[0][0]
    assert "Не удалось подключиться к Google Drive API" in alert_text

    with db_connection(tmp_db) as conn:
        folders = conn.execute("SELECT id FROM folders").fetchall()
        assert len(folders) == 1
        videos = conn.execute("SELECT id FROM videos").fetchall()
        assert len(videos) == 1
