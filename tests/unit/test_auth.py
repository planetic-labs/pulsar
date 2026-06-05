from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.auth import (
    get_session_token,
    is_jti_revoked,
    is_user_revoked,
    is_valid_token,
    login_user,
    require_access_token,
    revoke_session,
    revoke_user,
)


def test_get_session_token():
    request = MagicMock()
    request.query_params = {"token": "query-t"}
    request.session = {"access_token": "access-t", "token": "session-t"}
    request.cookies = {"access_token": "cookie-t"}
    request.headers = {"Authorization": "Bearer header-t"}

    # Priority 1: Query
    assert get_session_token(request) == "query-t"

    # Priority 2: Session (access_token)
    request.query_params = {}
    assert get_session_token(request) == "access-t"

    # Priority 2b: Session (token fallback)
    request.session = {"token": "session-t"}
    assert get_session_token(request) == "session-t"

    # Priority 3: Cookies
    request.session = {}
    assert get_session_token(request) == "cookie-t"

    # Priority 4: Headers
    request.cookies = {}
    assert get_session_token(request) == "header-t"


def test_require_access_token_success(monkeypatch):
    settings = MagicMock()
    settings.access_token = "valid-token"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)
    monkeypatch.setattr("app.auth.is_valid_token", lambda token: token == "valid-token")

    request = MagicMock()
    request.query_params = {"token": "valid-token"}
    request.session = {}

    assert require_access_token(request) == "valid-token"
    assert request.session["access_token"] == "valid-token"
    assert request.session["token"] == "valid-token"


def test_require_access_token_fail(monkeypatch):
    settings = MagicMock()
    settings.access_token = "valid-token"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)
    monkeypatch.setattr("app.auth.is_valid_token", lambda token: token == "valid-token")

    request = MagicMock()
    request.query_params = {"token": "wrong"}
    request.session = {}

    with pytest.raises(HTTPException) as exc:
        require_access_token(request)
    assert exc.value.status_code == 401


def test_login_user(monkeypatch):
    settings = MagicMock()
    settings.access_token = "master"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)
    monkeypatch.setattr("app.auth.is_valid_token", lambda token: token == "master")

    request = MagicMock()
    request.session = {}
    response = MagicMock()

    assert login_user(response, request, "master") is True
    assert request.session["access_token"] == "master"
    assert "token" in request.session
    response.set_cookie.assert_called_once()

    assert login_user(response, request, "wrong") is False


def test_is_valid_token_static(monkeypatch):
    settings = MagicMock()
    settings.access_token = "static-token"
    settings.ark_jwks_url = None
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    assert is_valid_token("static-token") is True
    assert is_valid_token("wrong-token") is False
    assert is_valid_token(None) is False


def test_is_valid_token_jwt(monkeypatch):
    settings = MagicMock()
    settings.access_token = "static-token"
    settings.ark_jwks_url = "https://mock-jwks.com/jwks.json"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    # Mock get_jwk_client
    mock_jwk_client = MagicMock()
    monkeypatch.setattr("app.auth.get_jwk_client", lambda: mock_jwk_client)

    # Mock jwt.decode
    mock_payload = {"sub": "user-123", "status": "active", "jti": "session-456"}
    monkeypatch.setattr("jwt.decode", lambda *args, **kwargs: mock_payload)

    # Mock db check to return False (not revoked)
    monkeypatch.setattr("app.auth.is_jti_revoked", lambda jti: False)
    monkeypatch.setattr("app.auth.is_user_revoked", lambda user_id: False)

    assert is_valid_token("some-jwt-token") is True


def test_is_valid_token_revoked_session(monkeypatch):
    settings = MagicMock()
    settings.access_token = "static-token"
    settings.ark_jwks_url = "https://mock-jwks.com/jwks.json"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    mock_jwk_client = MagicMock()
    monkeypatch.setattr("app.auth.get_jwk_client", lambda: mock_jwk_client)

    mock_payload = {"sub": "user-123", "status": "active", "jti": "session-456"}
    monkeypatch.setattr("jwt.decode", lambda *args, **kwargs: mock_payload)

    # Mock revoked check: session is revoked
    monkeypatch.setattr("app.auth.is_jti_revoked", lambda jti: True)
    monkeypatch.setattr("app.auth.is_user_revoked", lambda user_id: False)

    assert is_valid_token("some-jwt-token") is False


def test_is_valid_token_revoked_user(monkeypatch):
    settings = MagicMock()
    settings.access_token = "static-token"
    settings.ark_jwks_url = "https://mock-jwks.com/jwks.json"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    mock_jwk_client = MagicMock()
    monkeypatch.setattr("app.auth.get_jwk_client", lambda: mock_jwk_client)

    mock_payload = {"sub": "user-123", "status": "active", "jti": "session-456"}
    monkeypatch.setattr("jwt.decode", lambda *args, **kwargs: mock_payload)

    # Mock revoked check: user is revoked
    monkeypatch.setattr("app.auth.is_jti_revoked", lambda jti: False)
    monkeypatch.setattr("app.auth.is_user_revoked", lambda user_id: True)

    assert is_valid_token("some-jwt-token") is False


def test_is_valid_token_inactive(monkeypatch):
    settings = MagicMock()
    settings.access_token = "static-token"
    settings.ark_jwks_url = "https://mock-jwks.com/jwks.json"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    mock_jwk_client = MagicMock()
    monkeypatch.setattr("app.auth.get_jwk_client", lambda: mock_jwk_client)

    mock_payload = {"sub": "user-123", "status": "inactive", "jti": "session-456"}
    monkeypatch.setattr("jwt.decode", lambda *args, **kwargs: mock_payload)

    monkeypatch.setattr("app.auth.is_jti_revoked", lambda jti: False)
    monkeypatch.setattr("app.auth.is_user_revoked", lambda user_id: False)

    assert is_valid_token("some-jwt-token") is False


def test_db_revocation_helpers(monkeypatch, tmp_db):
    monkeypatch.setattr("app.config.get_sqlite_settings", lambda: tmp_db)

    # Verify initially not revoked
    assert is_jti_revoked("session-123") is False
    assert is_user_revoked("user-456") is False

    # Revoke
    revoke_session("session-123")
    revoke_user("user-456")

    # Verify revoked
    assert is_jti_revoked("session-123") is True
    assert is_user_revoked("user-456") is True


def test_perform_token_refresh_success(monkeypatch):
    settings = MagicMock()
    settings.ark_jwks_url = "https://mock-ark.com/.well-known/jwks.json"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    class MockResponse:
        status_code = 200

        def json(self):
            return {"access_token": "new-access", "refresh_token": "new-refresh"}

    mock_post = MagicMock(return_value=MockResponse())
    monkeypatch.setattr("httpx.Client.post", mock_post)

    from app.auth import perform_token_refresh

    res = perform_token_refresh("old-refresh")
    assert res == {"access_token": "new-access", "refresh_token": "new-refresh"}
    mock_post.assert_called_once_with(
        "https://mock-ark.com/api/v1/auth/refresh", json={"refresh_token": "old-refresh"}, timeout=10.0
    )


def test_perform_token_refresh_fail(monkeypatch):
    settings = MagicMock()
    settings.ark_jwks_url = "https://mock-ark.com/.well-known/jwks.json"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    class MockResponse:
        status_code = 401
        text = "Unauthorized"

    mock_post = MagicMock(return_value=MockResponse())
    monkeypatch.setattr("httpx.Client.post", mock_post)

    from app.auth import perform_token_refresh

    res = perform_token_refresh("old-refresh")
    assert res is None


def test_require_access_token_trigger_refresh(monkeypatch):
    settings = MagicMock()
    settings.access_token = "static-token"
    settings.ark_jwks_url = "https://mock-ark.com/.well-known/jwks.json"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    # Initially token is invalid
    monkeypatch.setattr("app.auth.is_valid_token", lambda token: token == "new-access")

    # Mock perform_token_refresh to return new tokens
    mock_refresh = MagicMock(return_value={"access_token": "new-access", "refresh_token": "new-refresh"})
    monkeypatch.setattr("app.auth.perform_token_refresh", mock_refresh)

    # Mock jwt.decode for require_access_token (decoding the new token)
    mock_payload = {"sub": "user-123", "status": "active", "jti": "session-456"}
    monkeypatch.setattr("jwt.decode", lambda *args, **kwargs: mock_payload)

    request = MagicMock()
    request.query_params = {}
    request.session = {"access_token": "expired-access", "refresh_token": "old-refresh"}

    token = require_access_token(request)

    assert token == "new-access"
    assert request.session["access_token"] == "new-access"
    assert request.session["refresh_token"] == "new-refresh"
    mock_refresh.assert_called_once_with("old-refresh")


def test_login_user_with_refresh_token(monkeypatch):
    settings = MagicMock()
    settings.access_token = "master"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)
    monkeypatch.setattr("app.auth.is_valid_token", lambda token: token == "master")

    request = MagicMock()
    request.session = {}
    response = MagicMock()

    assert login_user(response, request, "master", refresh_token="some-refresh-token") is True
    assert request.session["access_token"] == "master"
    assert request.session["refresh_token"] == "some-refresh-token"
    assert "token" in request.session
    response.set_cookie.assert_called_once()


def test_perform_token_refresh_server_error(monkeypatch):
    settings = MagicMock()
    settings.ark_jwks_url = "https://mock-ark.com/.well-known/jwks.json"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    class MockResponse:
        status_code = 500
        text = "Internal Server Error"

        def raise_for_status(self):
            import httpx
            raise httpx.HTTPStatusError("500", request=MagicMock(), response=self)

    mock_post = MagicMock(return_value=MockResponse())
    monkeypatch.setattr("httpx.Client.post", mock_post)

    import httpx
    import pytest

    from app.auth import perform_token_refresh

    with pytest.raises(httpx.HTTPStatusError):
        perform_token_refresh("old-refresh")


def test_require_access_token_network_error_retains_session(monkeypatch):
    import httpx
    settings = MagicMock()
    settings.access_token = "static-token"
    settings.ark_jwks_url = "https://mock-ark.com/.well-known/jwks.json"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    # Initially token is invalid
    monkeypatch.setattr("app.auth.is_valid_token", lambda token: False)

    # Mock perform_token_refresh to raise RequestError
    def mock_refresh_fail(*args, **kwargs):
        raise httpx.RequestError("Connection failed")
    monkeypatch.setattr("app.auth.perform_token_refresh", mock_refresh_fail)

    request = MagicMock()
    request.query_params = {}
    request.session = {"access_token": "expired-access", "refresh_token": "old-refresh"}

    import pytest
    from fastapi import HTTPException

    from app.auth import require_access_token

    with pytest.raises(HTTPException) as exc_info:
        require_access_token(request)

    assert exc_info.value.status_code == 503
    # Check that session was NOT cleared!
    assert request.session["access_token"] == "expired-access"
    assert request.session["refresh_token"] == "old-refresh"
