from unittest.mock import AsyncMock, MagicMock


def test_root_redirect_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith("/login")


def test_login_success(client):
    response = client.post("/login", data={"token": "test-token"}, follow_redirects=False)
    assert response.status_code == 303
    assert "session" in response.cookies


def test_login_fail(client):
    response = client.post("/login", data={"token": "wrong"}, follow_redirects=False)
    assert response.status_code == 200
    assert "Invalid access token" in response.text


def test_api_drive_ls_unauthorized(client):
    response = client.get("/api/drive/ls?folder_id=root")
    assert response.status_code == 401


def test_api_drive_ls_authorized(client, mocker):
    client.post("/login", data={"token": "test-token"})

    mock_instance = MagicMock()
    mock_instance.list_folder_contents = AsyncMock(return_value=[])
    mock_instance.get_shared_drives = AsyncMock(return_value=[])

    mocker.patch("app.main.GoogleDriveClient", return_value=mock_instance)

    response = client.get("/api/drive/ls?folder_id=root")
    assert response.status_code == 200
    assert response.json() == []


def test_api_tasks_ingest(client, mocker):
    client.post("/login", data={"token": "test-token"})
    mocker.patch("app.main.get_worker")

    response = client.post("/api/tasks/ingest", data={"file_id": "file123", "title": "My Video", "diarize": "true"})
    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_search_on_root(client, mocker, mock_qdrant):
    client.post("/login", data={"token": "test-token"})
    mock_hybrid = mocker.patch("app.main.hybrid_search", new_callable=AsyncMock)
    mock_hybrid.return_value = []

    response = client.get("/?q=test")
    assert response.status_code == 200
    assert "test" in response.text


def test_webhook_revocation_no_secret(client, monkeypatch):
    # Verify 501 is returned when secret is not configured
    settings = MagicMock()
    settings.ark_webhook_secret = None
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    response = client.post(
        "/api/v1/webhooks/revocation",
        json={"event": "session_revoked", "user_id": "user-123", "jti": "jti-456"},
    )
    assert response.status_code == 501


def test_webhook_revocation_success_session(client, monkeypatch, mocker):
    import hashlib
    import hmac

    # Mock settings with a secret key
    settings = MagicMock()
    settings.ark_webhook_secret = "test-webhook-secret"
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    # Mock db helpers so we don't need real db setups or can verify mock calls
    mock_revoke_session = mocker.patch("app.auth.revoke_session")
    mock_revoke_user = mocker.patch("app.auth.revoke_user")

    payload_data = {"event": "session_revoked", "user_id": "user-123", "jti": "jti-456"}
    import json

    raw_payload = json.dumps(payload_data).encode("utf-8")

    # Generate expected signature
    signature = hmac.new(b"test-webhook-secret", raw_payload, hashlib.sha256).hexdigest()

    response = client.post(
        "/api/v1/webhooks/revocation",
        content=raw_payload,
        headers={"X-Ark-Signature": signature, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_revoke_session.assert_called_once_with("jti-456")
    mock_revoke_user.assert_not_called()


def test_webhook_revocation_success_user(client, monkeypatch, mocker):
    import hashlib
    import hmac

    settings = MagicMock()
    settings.ark_webhook_secret = "test-webhook-secret"
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    mock_revoke_session = mocker.patch("app.auth.revoke_session")
    mock_revoke_user = mocker.patch("app.auth.revoke_user")

    payload_data = {"event": "session_revoked", "user_id": "user-123", "jti": None}
    import json

    raw_payload = json.dumps(payload_data).encode("utf-8")

    signature = hmac.new(b"test-webhook-secret", raw_payload, hashlib.sha256).hexdigest()

    response = client.post(
        "/api/v1/webhooks/revocation",
        content=raw_payload,
        headers={"X-Ark-Signature": signature, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_revoke_user.assert_called_once_with("user-123")
    mock_revoke_session.assert_not_called()


def test_webhook_revocation_invalid_signature(client, monkeypatch):
    settings = MagicMock()
    settings.ark_webhook_secret = "test-webhook-secret"
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    response = client.post(
        "/api/v1/webhooks/revocation",
        json={"event": "session_revoked", "user_id": "user-123", "jti": "jti-456"},
        headers={"X-Ark-Signature": "invalid-signature-value"},
    )
    assert response.status_code == 403


def test_webhook_revocation_missing_signature(client, monkeypatch):
    settings = MagicMock()
    settings.ark_webhook_secret = "test-webhook-secret"
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    response = client.post(
        "/api/v1/webhooks/revocation", json={"event": "session_revoked", "user_id": "user-123", "jti": "jti-456"}
    )
    assert response.status_code == 403


def test_api_auth_identify_not_configured(client, monkeypatch):
    settings = MagicMock()
    settings.ark_jwks_url = None
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    response = client.post("/api/v1/auth/identify", json={"email": "test@test.com"})
    assert response.status_code == 501


def test_api_auth_identify_success(client, monkeypatch, mocker):
    settings = MagicMock()
    settings.ark_jwks_url = "https://api.mock.com/.well-known/jwks.json"
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'{"next": "enter_code"}'

    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    mock_post.return_value = mock_response

    response = client.post("/api/v1/auth/identify", json={"email": "user@domain.com"})
    assert response.status_code == 200
    assert response.json() == {"next": "enter_code"}
    mock_post.assert_called_once_with(
        "https://api.mock.com/api/v1/auth/identify", json={"email": "user@domain.com"}, timeout=10.0
    )


def test_login_ark_success(client, monkeypatch, mocker):
    settings = MagicMock()
    settings.ark_jwks_url = "https://api.mock.com/.well-known/jwks.json"
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "next": "home",
        "access_token": "valid-mock-jwt-token",
        "refresh_token": "refresh-mock",
        "expires_in": 900,
    }

    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    mock_post.return_value = mock_response

    mocker.patch("app.main.is_valid_token", return_value=True)
    mock_login = mocker.patch("app.main.login_user", return_value=True)

    response = client.post("/login", data={"email": "user@domain.com", "code": "123456"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/")
    mock_login.assert_called_once()


def test_login_ark_fail_invalid_code(client, monkeypatch, mocker):
    settings = MagicMock()
    settings.ark_jwks_url = "https://api.mock.com/.well-known/jwks.json"
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.json.return_value = {"detail": "Invalid code or email"}

    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    mock_post.return_value = mock_response

    response = client.post("/login", data={"email": "user@domain.com", "code": "111111"}, follow_redirects=False)
    assert response.status_code == 200
    assert "Invalid code or email" in response.text


def test_api_restart_no_space_tasks(client, tmp_db, mocker):
    client.post("/login", data={"token": "test-token"})
    mocker.patch("app.main.get_worker")

    # Mock DB insert of a skipped task due to lack of space using tmp_db
    import json

    from app.db import db_connection

    with db_connection(tmp_db) as conn:
        conn.execute(
            "INSERT INTO tasks (task_type, payload, status) VALUES ('stage_1_download', ?, 'skipped_no_space')",
            (json.dumps({"file_id": "file123"}),),
        )

    response = client.post("/api/v1/tasks/restart_no_space")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "restarted"
    assert data["count"] == 1

    # Check it was set to pending in DB
    with db_connection(tmp_db) as conn:
        task = conn.execute("SELECT status FROM tasks WHERE task_type = 'stage_1_download'").fetchone()
        assert task["status"] == "pending"


def test_api_indexed_delete_video(client, tmp_db, mocker, mock_qdrant, tmp_path, monkeypatch):
    monkeypatch.setenv("APP_STORAGE_DIR", str(tmp_path))
    client.post("/login", data={"token": "test-token"})

    # Create dummy local files
    audio_dir = tmp_path / "audio"
    downloads_dir = tmp_path / "downloads"
    audio_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    dummy_video = downloads_dir / "drive_123.mp4"
    dummy_audio = audio_dir / "drive_123.ogg"

    dummy_video.write_text("video content")
    dummy_audio.write_text("audio content")

    from app.config import get_app_settings
    from app.db import db_connection

    app_settings = get_app_settings()
    dest_raw = app_settings.get_raw_transcript_path("drive_123")
    dest_norm = app_settings.get_normalized_transcript_path("drive_123")
    dest_raw.parent.mkdir(parents=True, exist_ok=True)
    dest_norm.parent.mkdir(parents=True, exist_ok=True)

    dest_raw.write_text("raw json")
    dest_norm.write_text("norm json")

    # Insert video, chunks
    with db_connection(tmp_db) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, status
            ) VALUES (?, ?, ?)
            """,
            ("drive_123", "Test Video", "indexed_chunks_ready"),
        )
        video_id = cursor.lastrowid

        cursor.execute(
            """
            INSERT INTO chunks (video_id, chunk_index, start_sec, end_sec, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (video_id, 0, 0.0, 10.0, "hello world"),
        )
        chunk_id = cursor.lastrowid

    # Mock get_manticore_client to return our mock_qdrant client
    mocker.patch("app.main.get_manticore_client", return_value=mock_qdrant)

    # Call delete API endpoint
    response = client.delete(f"/api/v1/indexed/videos/{video_id}")
    assert response.status_code == 200
    assert response.json() == {"status": "success"}

    # Verify database: check that video row and chunks are deleted
    with db_connection(tmp_db) as conn:
        video_row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        assert video_row is None

        chunk_row = conn.execute("SELECT * FROM chunks WHERE video_id = ?", (video_id,)).fetchone()
        assert chunk_row is None

    # Verify Manticore points deletion was called
    mock_qdrant.delete.assert_called_once_with(
        collection_name=mocker.ANY,
        ids=[chunk_id],
    )

    # Verify files are deleted from the filesystem
    assert not dummy_video.exists()
    assert not dummy_audio.exists()
    assert not dest_raw.exists()
    assert not dest_norm.exists()

    # Verify the raw transcript was successfully archived
    archived_file = tmp_path / "transcripts" / "archive" / f"video_{video_id}_drive_123.json.gz"
    assert archived_file.exists()
    assert archived_file.read_text() == "raw json"


def test_api_worker_duplicates_swap(client, tmp_db, mocker, mock_qdrant):
    client.post("/login", data={"token": "test-token"})
    mocker.patch("app.main.get_manticore_client", return_value=mock_qdrant)
    mocker.patch("app.main.get_worker")

    import json

    from app.db import db_connection

    # Insert two videos with the same MD5 (one original, one duplicate)
    with db_connection(tmp_db) as conn:
        cursor = conn.cursor()

        # 1. Original
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, original_id, md5_checksum,
                size_bytes, duration_sec, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "file_orig_id",
                "Orig Video",
                None,
                "swap-md5",
                500,
                30.0,
                "indexed_chunks_ready",
            ),
        )
        orig_id = cursor.lastrowid

        # 2. Duplicate
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, original_id, md5_checksum,
                status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("file_dup_id", "Dup Video", orig_id, "swap-md5", "skipped_duplicate_md5"),
        )
        dup_id = cursor.lastrowid

        # 3. Chunk
        cursor.execute(
            "INSERT INTO chunks (video_id, chunk_index, start_sec, end_sec, text) VALUES (?, ?, ?, ?, ?)",
            (orig_id, 0, 0.0, 10.0, "dummy chunk text"),
        )

    # Call swap roles endpoint
    response = client.post(
        "/api/v1/worker/duplicates/swap",
        data={"original_id": orig_id, "duplicate_id": dup_id},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"

    # Verify DB changes
    with db_connection(tmp_db) as conn:
        # Check original video (became duplicate)
        o_row = conn.execute("SELECT * FROM videos WHERE id = ?", (orig_id,)).fetchone()
        assert o_row["original_id"] == dup_id
        assert o_row["status"] == "skipped_duplicate_md5"
        assert o_row["size_bytes"] is None
        assert o_row["duration_sec"] is None

        # Check duplicate video (became original)
        d_row = conn.execute("SELECT * FROM videos WHERE id = ?", (dup_id,)).fetchone()
        assert d_row["original_id"] is None
        assert d_row["status"] == "transcribed"
        assert d_row["size_bytes"] == 500
        assert d_row["duration_sec"] == 30.0

        # Check chunks mapped to new video_id
        c_row = conn.execute("SELECT * FROM chunks WHERE video_id = ?", (dup_id,)).fetchone()
        assert c_row is not None

        # Check Qdrant indexing task was queued
        task = conn.execute("SELECT * FROM tasks WHERE task_type = 'stage_3_index' AND status = 'pending'").fetchone()
        assert task is not None
        task_payload = json.loads(task["payload"])
        assert task_payload["video_id"] == dup_id

    # Verify Manticore points deletion for old original ID A
    mock_qdrant.delete.assert_called_once_with(
        collection_name=mocker.ANY,
        where_clause=f"video_id = {orig_id}",
    )


def test_api_indexed_ls(client, tmp_db):
    client.post("/login", data={"token": "test-token"})

    from app.db import db_connection

    with db_connection(tmp_db) as conn:
        cursor = conn.cursor()
        # 1. Create a parent folder
        cursor.execute(
            "INSERT INTO folders (id, name, parent_id) VALUES (?, ?, ?)",
            ("folder_1", "Test Folder", None),
        )
        # 2. Original video inside that folder
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, md5_checksum,
                size_bytes, duration_sec, status, parent_folder_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "file_orig_id",
                "Orig Video",
                "some-md5",
                500,
                30.0,
                "indexed_chunks_ready",
                "folder_1",
            ),
        )
        orig_id = cursor.lastrowid

        # 3. Duplicate video inside that folder
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, original_id, md5_checksum,
                status, parent_folder_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "file_dup_id",
                "Dup Video",
                orig_id,
                "some-md5",
                "skipped_duplicate_md5",
                "folder_1",
            ),
        )

    # List root
    response = client.get("/api/v1/indexed/ls")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    # Should list folder_1
    folders = [item for item in data["items"] if item["is_folder"]]
    assert len(folders) == 1
    assert folders[0]["id"] == "folder_1"
    assert folders[0]["name"] == "Test Folder"

    # List folder_1
    response = client.get("/api/v1/indexed/ls?folder_id=folder_1")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data

    videos = [item for item in data["items"] if not item["is_folder"]]
    assert len(videos) == 2
    # Sort by name
    videos.sort(key=lambda x: x["name"])

    # Check Dup Video
    assert videos[0]["name"] == "Dup Video"
    assert videos[0]["is_md5_duplicate"] is True

    # Check Orig Video
    assert videos[1]["name"] == "Orig Video"
    assert videos[1]["is_md5_duplicate"] is False


def test_api_worker_duplicates_save(client, tmp_db):
    client.post("/login", data={"token": "test-token"})

    from app.db import db_connection

    # Insert a duplicate video
    with db_connection(tmp_db) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, status
            ) VALUES (?, ?, ?)
            """,
            ("file_orig_id", "Orig Video", "indexed_chunks_ready"),
        )
        orig_id = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, original_id, status
            ) VALUES (?, ?, ?, ?)
            """,
            ("file_dup_id", "Dup Video", orig_id, "skipped_duplicate_md5"),
        )
        dup_id = cursor.lastrowid

    # Call save endpoint
    response = client.post(
        "/api/v1/worker/duplicates/save",
        data={"duplicate_id": dup_id},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"

    # Verify the video still exists and is a duplicate
    with db_connection(tmp_db) as conn:
        row = conn.execute("SELECT original_id FROM videos WHERE id = ?", (dup_id,)).fetchone()
        assert row["original_id"] == orig_id


def test_check_integrity_duplicates(tmp_db, mocker, monkeypatch):
    from scripts.verify_integrity import verify_integrity as check_integrity

    # Mock settings and Qdrant client to avoid side effects
    monkeypatch.setattr("scripts.verify_integrity.get_sqlite_settings", lambda: tmp_db)
    mock_qdrant = mocker.patch("scripts.verify_integrity.get_manticore_client")
    mock_qdrant.return_value.scroll.return_value = ([], None)
    mocker.patch("scripts.verify_integrity.get_manticore_settings")

    from app.db import db_connection

    # Insert a duplicate video that has a chunk (this is a logic error!)
    with db_connection(tmp_db) as conn:
        # Temporarily disable foreign keys to insert an orphan duplicate reference
        conn.execute("PRAGMA foreign_keys = OFF")
        # Temporarily drop unique index on md5 to insert duplicate originals
        conn.execute("DROP INDEX IF EXISTS uidx_videos_md5_original")

        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, original_id, md5_checksum,
                status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("file_dup_id", "Dup Video", 9999, "test-md5", "skipped_duplicate_md5"),
        )
        dup_id = cursor.lastrowid

        # Insert a chunk for this duplicate (creates duplicate has chunks issue)
        cursor.execute(
            "INSERT INTO chunks (video_id, chunk_index, start_sec, end_sec, text) "
            "VALUES (?, 0, 0.0, 10.0, 'corrupted chunk')",
            (dup_id,),
        )

        # Also insert two originals with the same MD5 (creates another issue)
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, md5_checksum,
                status
            ) VALUES (?, ?, ?, 'completed')
            """,
            ("file_orig1_id", "Orig 1", "same-md5"),
        )
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, md5_checksum,
                status
            ) VALUES (?, ?, ?, 'completed')
            """,
            ("file_orig2_id", "Orig 2", "same-md5"),
        )

        conn.execute("PRAGMA foreign_keys = ON")

    # Run check_integrity
    res = check_integrity()

    # Check issues list
    issues = res["issues"]
    assert any("Duplicate video 'Dup Video'" in i for i in issues)
    assert any("Multiple original videos share the same MD5 checksum 'same-md5'" in i for i in issues)
    assert any("Orphan duplicate video 'Dup Video'" in i for i in issues)


def test_api_indexed_toggle_short(client, tmp_db, tmp_path, mocker, mock_qdrant):
    import gzip
    import json

    client.post("/login", data={"token": "test-token"})

    from app.config import get_app_settings
    from app.db import db_connection

    app_settings = get_app_settings()
    norm_path = app_settings.get_normalized_transcript_path("file_video_id")
    norm_path.parent.mkdir(parents=True, exist_ok=True)

    dummy_data = {
        "utterances": [
            {"start": 0.0, "end": 10.0, "text": "a" * 300},
            {"start": 10.0, "end": 20.0, "text": "b" * 300},
            {"start": 20.0, "end": 30.0, "text": "c" * 10},
        ]
    }
    with gzip.open(norm_path, "wt", encoding="utf-8") as f:
        json.dump(dummy_data, f)

    # Insert a video
    with db_connection(tmp_db) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, status, is_short
            ) VALUES (?, ?, ?, ?)
            """,
            ("file_video_id", "Toggle Video", "transcribed", 0),
        )
        video_id = cursor.lastrowid

        # Insert some initial chunks
        cursor.execute(
            """
            INSERT INTO chunks (video_id, chunk_index, start_sec, end_sec, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (video_id, 0, 0.0, 10.0, "initial chunk"),
        )

    # Mock Manticore client
    mocker.patch("app.main.get_manticore_client", return_value=mock_qdrant)

    # Call toggle_short (make it short)
    response = client.post(f"/api/v1/indexed/videos/{video_id}/toggle_short")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["is_short"] is True

    # Verify DB state: is_short is True, and chunk is now a single chunk
    with db_connection(tmp_db) as conn:
        row = conn.execute("SELECT is_short FROM videos WHERE id = ?", (video_id,)).fetchone()
        assert row["is_short"] == 1

        chunks = conn.execute(
            "SELECT chunk_index, start_sec, end_sec, text FROM chunks WHERE video_id = ?", (video_id,)
        ).fetchall()
        assert len(chunks) == 1
        assert chunks[0]["chunk_index"] == 0
        assert chunks[0]["start_sec"] == 0.0

    # Call toggle_short again (make it not short)
    response = client.post(f"/api/v1/indexed/videos/{video_id}/toggle_short")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["is_short"] is False

    # Verify DB state: is_short is False, and chunks are split again (2 chunks)
    with db_connection(tmp_db) as conn:
        row = conn.execute("SELECT is_short FROM videos WHERE id = ?", (video_id,)).fetchone()
        assert row["is_short"] == 0

        chunks = conn.execute(
            "SELECT chunk_index, start_sec, end_sec, text FROM chunks WHERE video_id = ?", (video_id,)
        ).fetchall()
        assert len(chunks) == 2
        assert chunks[0]["chunk_index"] == 0
        assert chunks[1]["chunk_index"] == 1


def test_check_integrity_chunk_mismatch(tmp_db, mocker, monkeypatch, tmp_path):
    import gzip
    import json

    from app.config import get_app_settings
    from scripts.verify_integrity import verify_integrity as check_integrity

    # Mock settings and Qdrant client to avoid side effects
    monkeypatch.setattr("scripts.verify_integrity.get_sqlite_settings", lambda: tmp_db)
    mock_qdrant = mocker.patch("scripts.verify_integrity.get_manticore_client")
    mock_qdrant.return_value.scroll.return_value = ([], None)
    mocker.patch("scripts.verify_integrity.get_manticore_settings")

    from app.db import db_connection

    # Create dummy transcript files that require 2 chunks if is_short is False
    dummy_data = {
        "utterances": [
            {"start": 0.0, "end": 10.0, "text": "a" * 300},
            {"start": 10.0, "end": 20.0, "text": "b" * 300},
            {"start": 20.0, "end": 30.0, "text": "c" * 10},
        ]
    }

    app_settings = get_app_settings()
    raw_path = app_settings.get_raw_transcript_path("file_video_id")
    norm_path = app_settings.get_normalized_transcript_path("file_video_id")

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    norm_path.parent.mkdir(parents=True, exist_ok=True)

    with gzip.open(raw_path, "wt", encoding="utf-8") as f:
        json.dump(dummy_data, f)
    with gzip.open(norm_path, "wt", encoding="utf-8") as f:
        json.dump(dummy_data, f)

    # Insert a video
    with db_connection(tmp_db) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO videos (
                source_file_id, title, status, is_short
            ) VALUES (?, ?, ?, ?)
            """,
            ("file_video_id", "Integrity Video", "completed", 0),
        )
        video_id = cursor.lastrowid

        # Insert only 1 chunk in SQLite (causes a mismatch since 2 chunks are expected)
        cursor.execute(
            """
            INSERT INTO chunks (video_id, chunk_index, start_sec, end_sec, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (video_id, 0, 0.0, 10.0, "initial chunk"),
        )

    # Run check_integrity
    res = check_integrity()

    # Check issues list
    issues = res["issues"]
    assert any("chunk count mismatch. DB has 1, expected 2" in i for i in issues)

    # Verify SQLite was auto-healed (now has 2 chunks)
    with db_connection(tmp_db) as conn:
        chunks = conn.execute(
            "SELECT chunk_index, start_sec, end_sec FROM chunks WHERE video_id = ?", (video_id,)
        ).fetchall()
        assert len(chunks) == 2
        assert chunks[0]["chunk_index"] == 0
        assert chunks[1]["chunk_index"] == 1

        # Verify task was added
        task = conn.execute("SELECT task_type, payload FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
        assert task["task_type"] == "stage_3_index"
        payload = json.loads(task["payload"])
        assert payload["video_id"] == video_id
