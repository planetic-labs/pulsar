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
    dummy_video = tmp_path / "video.mp4"
    dummy_audio = tmp_path / "audio.ogg"
    dummy_raw_json = tmp_path / "raw.json"
    dummy_norm_json = tmp_path / "norm.json"

    dummy_video.write_text("video content")
    dummy_audio.write_text("audio content")
    dummy_raw_json.write_text("raw json")
    dummy_norm_json.write_text("norm json")

    from app.db import db_connection

    # Insert video, transcript, chunks, speakers
    with db_connection(tmp_db) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO videos (
                source_type, source_file_id, title, local_video_path, local_audio_path, processing_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("google_drive", "drive_123", "Test Video", str(dummy_video), str(dummy_audio), "indexed_chunks_ready"),
        )
        video_id = cursor.lastrowid

        cursor.execute(
            """
            INSERT INTO transcripts (video_id, language, raw_json_path, normalized_json_path)
            VALUES (?, ?, ?, ?)
            """,
            (video_id, "ru", str(dummy_raw_json), str(dummy_norm_json)),
        )
        transcript_id = cursor.lastrowid

        cursor.execute(
            """
            INSERT INTO chunks (video_id, transcript_id, chunk_index, start_sec, end_sec, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (video_id, transcript_id, 0, 0.0, 10.0, "hello world"),
        )
        chunk_id = cursor.lastrowid

    # Mock get_qdrant_client to return our mock_qdrant client
    mocker.patch("app.main.get_qdrant_client", return_value=mock_qdrant)

    # Call delete API endpoint
    response = client.delete(f"/api/v1/indexed/videos/{video_id}")
    assert response.status_code == 200
    assert response.json() == {"status": "success"}

    # Verify database: check that video row, transcripts, and chunks are deleted
    with db_connection(tmp_db) as conn:
        video_row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        assert video_row is None

        transcript_row = conn.execute("SELECT * FROM transcripts WHERE video_id = ?", (video_id,)).fetchone()
        assert transcript_row is None

        chunk_row = conn.execute("SELECT * FROM chunks WHERE video_id = ?", (video_id,)).fetchone()
        assert chunk_row is None

    # Verify Qdrant points deletion was called
    mock_qdrant.delete.assert_called_once_with(
        collection_name=mocker.ANY,
        points_selector=mocker.ANY,
    )
    # Check that points_selector includes chunk_id
    call_args = mock_qdrant.delete.call_args[1]
    selector = call_args["points_selector"]
    assert selector.points == [chunk_id]

    # Verify files are deleted from the filesystem
    assert not dummy_video.exists()
    assert not dummy_audio.exists()
    assert not dummy_raw_json.exists()
    assert not dummy_norm_json.exists()

    # Verify the raw transcript was successfully archived
    archived_file = tmp_path / "transcripts" / "archive" / f"video_{video_id}_{dummy_raw_json.name}"
    assert archived_file.exists()
    assert archived_file.read_text() == "raw json"


def test_api_worker_duplicates_swap(client, tmp_db, mocker, mock_qdrant):
    client.post("/login", data={"token": "test-token"})
    mocker.patch("app.main.get_qdrant_client", return_value=mock_qdrant)
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
                source_type, source_file_id, title, is_md5_duplicate, md5_checksum,
                size_bytes, duration_sec, processing_status, local_audio_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "google_drive",
                "file_orig_id",
                "Orig Video",
                0,
                "swap-md5",
                500,
                30.0,
                "indexed_chunks_ready",
                "audio.ogg",
            ),
        )
        orig_id = cursor.lastrowid

        # 2. Duplicate
        cursor.execute(
            """
            INSERT INTO videos (
                source_type, source_file_id, title, is_md5_duplicate, md5_checksum,
                processing_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("google_drive", "file_dup_id", "Dup Video", 1, "swap-md5", "skipped_duplicate_md5"),
        )
        dup_id = cursor.lastrowid

        # 3. Transcript
        cursor.execute(
            "INSERT INTO transcripts (video_id, language) VALUES (?, ?)",
            (orig_id, "ru"),
        )
        trans_id = cursor.lastrowid

        # 4. Chunk
        cursor.execute(
            "INSERT INTO chunks (video_id, transcript_id, chunk_index, "
            "start_sec, end_sec, text) VALUES (?, ?, ?, ?, ?, ?)",
            (orig_id, trans_id, 0, 0.0, 10.0, "dummy chunk text"),
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
        assert o_row["is_md5_duplicate"] == 1
        assert o_row["processing_status"] == "skipped_duplicate_md5"
        assert o_row["size_bytes"] is None
        assert o_row["duration_sec"] is None
        assert o_row["local_audio_path"] is None

        # Check duplicate video (became original)
        d_row = conn.execute("SELECT * FROM videos WHERE id = ?", (dup_id,)).fetchone()
        assert d_row["is_md5_duplicate"] == 0
        assert d_row["processing_status"] == "transcribed"
        assert d_row["size_bytes"] == 500
        assert d_row["duration_sec"] == 30.0
        assert d_row["local_audio_path"] == "audio.ogg"

        # Check transcript and chunks mapped to new video_id
        t_row = conn.execute("SELECT * FROM transcripts WHERE video_id = ?", (dup_id,)).fetchone()
        assert t_row is not None
        c_row = conn.execute("SELECT * FROM chunks WHERE video_id = ?", (dup_id,)).fetchone()
        assert c_row is not None

        # Check Qdrant indexing task was queued
        task = conn.execute("SELECT * FROM tasks WHERE task_type = 'stage_3_index' AND status = 'pending'").fetchone()
        assert task is not None
        task_payload = json.loads(task["payload"])
        assert task_payload["video_id"] == dup_id

    # Verify Qdrant points deletion for old original ID A
    mock_qdrant.delete.assert_called_once()


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
                source_type, source_file_id, title, is_md5_duplicate, md5_checksum,
                size_bytes, duration_sec, processing_status, parent_folder_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "google_drive",
                "file_orig_id",
                "Orig Video",
                0,
                "some-md5",
                500,
                30.0,
                "indexed_chunks_ready",
                "folder_1",
            ),
        )
        # 3. Duplicate video inside that folder
        cursor.execute(
            """
            INSERT INTO videos (
                source_type, source_file_id, title, is_md5_duplicate, md5_checksum,
                processing_status, parent_folder_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "google_drive",
                "file_dup_id",
                "Dup Video",
                1,
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
