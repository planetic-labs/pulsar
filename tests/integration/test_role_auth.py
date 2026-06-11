from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest


@pytest.fixture
def user_token():
    # Use a 32-byte key to prevent InsecureKeyLengthWarning from PyJWT
    return jwt.encode(
        {"sub": "user-123", "status": "active"},
        "super_secret_session_token_key_for_testing_32_bytes",
        algorithm="HS256",
    )


def test_admin_access_pages(client):
    # Log in as admin (test-token is configured in conftest.py)
    client.post("/login", data={"token": "test-token"})

    # Check admin pages return 200 OK
    for path in ["/import", "/status", "/indexed"]:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200


def test_user_access_pages_redirect(client, user_token, monkeypatch):
    # Mock is_valid_token in both auth and main modules so the user_token is accepted
    monkeypatch.setattr("app.auth.is_valid_token", lambda t: t == user_token)
    monkeypatch.setattr("app.main.is_valid_token", lambda t: t == user_token)

    # Log in with the user token
    client.post("/login", data={"token": user_token})

    # Check user is redirected from admin pages to the main search page (/)
    for path in ["/import", "/status", "/indexed"]:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/"


def test_admin_api_endpoints(client, mocker):
    client.post("/login", data={"token": "test-token"})

    # Mock get_worker
    mock_worker = MagicMock()
    mock_worker.is_running = False  # Set as boolean attribute, not mock callable return_value
    mock_worker.run = AsyncMock()  # Mock run as an async function so it returns a coroutine
    mocker.patch("app.main.get_worker", return_value=mock_worker)

    # Start worker should succeed (returns starting state)
    response = client.post("/api/v1/worker/start")
    assert response.status_code == 200
    assert response.json() == {"status": "starting"}

    # Sync indexed files should succeed (mocking sync_indexed_metadata returning int 0)
    mock_sync = mocker.patch("scripts.sync_titles.sync_indexed_metadata", new_callable=AsyncMock)
    mock_sync.return_value = 0
    response = client.post("/api/v1/indexed/sync")
    assert response.status_code == 200
    assert response.json() == {"status": "success", "updated_count": 0}


def test_user_api_endpoints_forbidden(client, user_token, monkeypatch, mocker):
    monkeypatch.setattr("app.auth.is_valid_token", lambda t: t == user_token)
    monkeypatch.setattr("app.main.is_valid_token", lambda t: t == user_token)
    client.post("/login", data={"token": user_token})

    # Try starting the worker (should fail with 403 Forbidden)
    response = client.post("/api/v1/worker/start")
    assert response.status_code == 403
    assert "Admin role required" in response.json()["detail"]

    # Try triggering sync (should fail with 403 Forbidden)
    response = client.post("/api/v1/indexed/sync")
    assert response.status_code == 403
    assert "Admin role required" in response.json()["detail"]

    # Try deleting a video (should fail with 403 Forbidden)
    response = client.delete("/api/v1/indexed/videos/42")
    assert response.status_code == 403
    assert "Admin role required" in response.json()["detail"]


def test_user_can_access_video_chunks(client, user_token, monkeypatch, tmp_db):
    monkeypatch.setattr("app.auth.is_valid_token", lambda t: t == user_token)
    monkeypatch.setattr("app.main.is_valid_token", lambda t: t == user_token)
    monkeypatch.setattr("app.main.get_sqlite_settings", lambda: tmp_db)
    client.post("/login", data={"token": user_token})

    # Insert a dummy video in the database with NOT NULL fields populated
    from app.db import db_connection

    with db_connection(tmp_db) as conn:
        conn.execute(
            """
            INSERT INTO videos (
                id, title, mime_type, source_file_id, status
            ) VALUES (42, 'Test Video', 'video/mp4', 'file42', 'indexed_chunks_ready')
            """
        )

    # Access video chunks endpoint as user
    response = client.get("/api/videos/42/chunks")
    assert response.status_code == 200
    assert response.json() == []
