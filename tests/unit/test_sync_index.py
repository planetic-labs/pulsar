import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db import db_connection
from scripts.sync_index import main as sync_main
from scripts.sync_index import scan_folder_recursive, send_telegram_notification


@pytest.mark.asyncio
async def test_send_telegram_notification(monkeypatch, mocker):
    # Mock global variables inside the scripts.sync_index module
    monkeypatch.setattr("scripts.sync_index.TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setattr("scripts.sync_index.TELEGRAM_CHAT_ID", "7890")

    # Mock httpx AsyncClient post
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
async def test_scan_folder_recursive():
    drive_mock = MagicMock()

    # Mock folder contents dynamically to prevent infinite recursion
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
                {"id": "text_id", "name": "Notes.txt", "is_folder": False, "mime_type": "text/plain"},
            ]
        return []

    drive_mock.list_folder_contents = mock_list

    visited_folders = []
    drive_files = []

    # Call scan_folder_recursive
    await scan_folder_recursive(
        drive=drive_mock,
        folder_id="root_id",
        parent_id=None,
        folder_name="Root",
        visited_folders=visited_folders,
        drive_files=drive_files,
    )

    assert {"id": "root_id", "name": "Root", "parent_id": None} in visited_folders
    assert {"id": "subfolder_id", "name": "Subfolder", "parent_id": "root_id"} in visited_folders
    assert len(drive_files) == 1
    assert drive_files[0]["file_id"] == "video_id"
    assert drive_files[0]["name"] == "Video.mp4"


@pytest.mark.asyncio
async def test_sync_main_flow(tmp_db, monkeypatch, mocker):
    # Setup test env settings and database
    monkeypatch.setattr("scripts.sync_index.get_sqlite_settings", lambda: tmp_db)

    # Pre-populate internal database with:
    # 1. A root folder to scan
    # 2. A subfolder to delete (parent is root_folder_id so it isn't scanned directly as root)
    # 3. An existing video that is updated
    # 4. An existing video that was removed (to trigger Telegram alert)
    # 5. An existing video that remains unchanged
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
                id, source_file_id, title, parent_folder_id, recorded_date, is_4k, processing_status, source_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                id, source_file_id, title, parent_folder_id, processing_status, source_type, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
        # Existing video that remains unchanged
        conn.execute(
            """
            INSERT INTO videos (
                id, source_file_id, title, parent_folder_id, recorded_date, is_4k, processing_status, source_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    # - New video to queue: "new_video_id" (title: "Brand New Video.mp4")
    async def mock_list_contents(folder_id, use_cache=False):
        if folder_id == "root_folder_id":
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
                {"id": "new_video_id", "name": "Brand New Video.mp4", "is_folder": False, "mime_type": "video/mp4"},
            ]
        elif folder_id == "new_subfolder_id":
            return []
        return []

    drive_mock.list_folder_contents = mock_list_contents
    mocker.patch("scripts.sync_index.GoogleDriveClient", return_value=drive_mock)

    # Mock Telegram notification
    mock_telegram = mocker.patch("scripts.sync_index.send_telegram_notification", new_callable=AsyncMock)

    # Run main sync logic
    await sync_main()

    # Assertions
    with db_connection(tmp_db) as conn:
        # 1. Folders:
        # - "root_folder_id" must exist
        # - "new_subfolder_id" must be created
        # - "old_folder_to_delete" must be deleted
        folders = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM folders").fetchall()}
        assert "root_folder_id" in folders
        assert "new_subfolder_id" in folders
        assert "old_folder_to_delete" not in folders

        # 2. Updated video metadata checks:
        # - Title updated to: 2026.05.20 New Title 4K.mp4
        # - is_4k is 1 (Russian K or English K matched by 4[KК])
        # - recorded_date extracted to 2026-05-20
        updated_video = conn.execute(
            "SELECT title, recorded_date, is_4k FROM videos WHERE source_file_id = 'video_to_update'"
        ).fetchone()
        assert updated_video["title"] == "2026.05.20 New Title 4K.mp4"
        assert updated_video["recorded_date"] == "2026-05-20"
        assert updated_video["is_4k"] == 1

        # Check that re-indexing task (stage_3_index) was queued for video_to_update
        tasks = conn.execute("SELECT task_type, payload FROM tasks").fetchall()
        stage_3_tasks = [t for t in tasks if t["task_type"] == "stage_3_index"]
        assert len(stage_3_tasks) == 1
        payload = json.loads(stage_3_tasks[0]["payload"])
        assert payload["video_id"] == 101
        assert payload["title"] == "2026.05.20 New Title 4K.mp4"

        # 3. New video checks:
        # - A "stage_1_download" task must be queued for "new_video_id"
        download_tasks = [t for t in tasks if t["task_type"] == "stage_1_download"]
        assert len(download_tasks) == 1
        dl_payload = json.loads(download_tasks[0]["payload"])
        assert dl_payload["file_id"] == "new_video_id"
        assert dl_payload["title"] == "Brand New Video.mp4"

    # 4. Removed video checks:
    # - send_telegram_notification must be called with details of "video_removed"
    mock_telegram.assert_called_once()
    alert_args = mock_telegram.call_args[0][0]
    assert len(alert_args) == 1
    assert alert_args[0]["file_id"] == "video_removed"
    assert alert_args[0]["title"] == "2020.02.02 Removed Video.mp4"
