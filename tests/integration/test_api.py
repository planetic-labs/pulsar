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
    import hmac
    import hashlib

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
    signature = hmac.new(
        b"test-webhook-secret",
        raw_payload,
        hashlib.sha256
    ).hexdigest()

    response = client.post(
        "/api/v1/webhooks/revocation",
        content=raw_payload,
        headers={"X-Ark-Signature": signature, "Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_revoke_session.assert_called_once_with("jti-456")
    mock_revoke_user.assert_not_called()


def test_webhook_revocation_success_user(client, monkeypatch, mocker):
    import hmac
    import hashlib

    settings = MagicMock()
    settings.ark_webhook_secret = "test-webhook-secret"
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    mock_revoke_session = mocker.patch("app.auth.revoke_session")
    mock_revoke_user = mocker.patch("app.auth.revoke_user")

    payload_data = {"event": "session_revoked", "user_id": "user-123", "jti": None}
    import json
    raw_payload = json.dumps(payload_data).encode("utf-8")

    signature = hmac.new(
        b"test-webhook-secret",
        raw_payload,
        hashlib.sha256
    ).hexdigest()

    response = client.post(
        "/api/v1/webhooks/revocation",
        content=raw_payload,
        headers={"X-Ark-Signature": signature, "Content-Type": "application/json"}
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
        headers={"X-Ark-Signature": "invalid-signature-value"}
    )
    assert response.status_code == 403


def test_webhook_revocation_missing_signature(client, monkeypatch):
    settings = MagicMock()
    settings.ark_webhook_secret = "test-webhook-secret"
    monkeypatch.setattr("app.main.get_app_settings", lambda: settings)

    response = client.post(
        "/api/v1/webhooks/revocation",
        json={"event": "session_revoked", "user_id": "user-123", "jti": "jti-456"}
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
        "https://api.mock.com/api/v1/auth/identify",
        json={"email": "user@domain.com"},
        timeout=10.0
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
        "expires_in": 900
    }

    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    mock_post.return_value = mock_response

    mocker.patch("app.main.is_valid_token", return_value=True)
    mock_login = mocker.patch("app.main.login_user", return_value=True)

    response = client.post(
        "/login",
        data={"email": "user@domain.com", "code": "123456"},
        follow_redirects=False
    )
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

    response = client.post(
        "/login",
        data={"email": "user@domain.com", "code": "111111"},
        follow_redirects=False
    )
    assert response.status_code == 200
    assert "Invalid code or email" in response.text

