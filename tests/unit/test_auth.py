from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.auth import get_session_token, login_user, require_access_token


def test_get_session_token():
    request = MagicMock()
    request.query_params = {"token": "query-t"}
    request.session = {"token": "session-t"}
    request.cookies = {"access_token": "cookie-t"}

    # Priority 1: Query
    assert get_session_token(request) == "query-t"

    # Priority 2: Session
    request.query_params = {}
    assert get_session_token(request) == "session-t"

    # Priority 3: Cookies
    request.session = {}
    assert get_session_token(request) == "cookie-t"


def test_require_access_token_success(monkeypatch):
    settings = MagicMock()
    settings.access_token = "valid-token"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

    request = MagicMock()
    request.query_params = {"token": "valid-token"}
    request.session = {}

    assert require_access_token(request) == "valid-token"
    assert request.session["token"] == "valid-token"


def test_require_access_token_fail(monkeypatch):
    settings = MagicMock()
    settings.access_token = "valid-token"
    monkeypatch.setattr("app.auth.get_app_settings", lambda: settings)

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

    request = MagicMock()
    request.session = {}
    response = MagicMock()

    assert login_user(response, request, "master") is True
    assert request.session["token"] == "master"
    response.set_cookie.assert_called_once()

    assert login_user(response, request, "wrong") is False
