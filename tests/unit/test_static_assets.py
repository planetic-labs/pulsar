from __future__ import annotations

from app import core


def test_static_asset_url_includes_release_version(monkeypatch) -> None:
    monkeypatch.setattr(core, "APP_VERSION", "2026.09.11.4")

    assert core.static_asset_url("js/search_app.js") == "/static/js/search_app.js?v=2026.09.11.4"


def test_static_asset_url_normalizes_leading_slash(monkeypatch) -> None:
    monkeypatch.setattr(core, "APP_VERSION", "release candidate")

    assert core.static_asset_url("/css/app.css") == "/static/css/app.css?v=release%20candidate"
