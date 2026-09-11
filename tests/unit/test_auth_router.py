from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from app.routers import auth


@pytest.mark.asyncio
async def test_identify_returns_human_readable_error_when_ark_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b'{"email":"user@example.com"}', "more_body": False}

    async def unavailable(*args: object, **kwargs: object) -> None:
        request = httpx.Request("POST", "http://api:8354/api/v1/auth/identify")
        raise httpx.ConnectError("All connection attempts failed", request=request)

    monkeypatch.setattr(auth, "get_app_settings", lambda: SimpleNamespace(ark_jwks_url="http://api:8354/jwks.json"))
    monkeypatch.setattr(httpx.AsyncClient, "post", unavailable)

    request = Request({"type": "http", "method": "POST", "path": "/api/v1/auth/identify", "headers": []}, receive)
    response = await auth.api_auth_identify(request)

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "error": "authorization_service_unavailable",
        "message": "Сервис авторизации временно недоступен. Попробуйте ещё раз позже.",
    }
