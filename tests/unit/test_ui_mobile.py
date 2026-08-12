from __future__ import annotations

import pytest
from starlette.requests import Request

from app.core import templates
from app.routers.ui import is_mobile_request


@pytest.mark.parametrize(
    "user_agent,expected",
    [
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)", True),
        ("Mozilla/5.0 (Linux; Android 13; Pixel 7)", True),
        ("Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X)", True),
        ("Mozilla/5.0 (Linux; Mobile; rv:109.0) Gecko/119.0 Firefox/119.0", True),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", False),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36", False),
        ("", False),
    ],
)
def test_is_mobile_request(user_agent: str, expected: bool):
    scope = {
        "type": "http",
        "headers": [(b"user-agent", user_agent.encode("utf-8"))],
    }
    request = Request(scope)
    assert is_mobile_request(request) is expected


def test_index_mobile_template_renders():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
    }
    request = Request(scope)
    context = {
        "request": request,
        "query": "тестовый запрос",
        "results": [
            {
                "video_id": 1,
                "title": "Тестовое видео",
                "start_sec": 10.0,
                "end_sec": 25.0,
                "start_ts": "00:10",
                "end_ts": "00:25",
                "chunk_id": 101,
                "text": "Это тестовый фрагмент транскрипта",
                "match_type": "hybrid",
                "speaker": "Спикер 1",
                "source_url": "https://drive.google.com/file/d/test/view",
                "is_flagged": False,
            }
        ],
        "mode": "hybrid",
        "date_from": "2024-01-01",
        "date_to": "2026-08-12",
        "today_val": "2026-08-12",
        "default_start": "2020-01-01",
        "video_type": "all",
        "token": "test-token",
        "stats": {
            "total_videos": 42,
            "total_hours": 12.5,
            "version": "2026.07.12",
            "worker_busy": False,
        },
    }
    response = templates.TemplateResponse(request, "index_mobile.html", context)
    content = bytes(response.body).decode("utf-8")
    assert "Pulsar AI" in content
    assert "Тестовое видео" in content
    assert "00:10 — 00:25" in content
    assert "Drive ↗" in content
    assert "Спикер 1" in content
