from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app import config
from app.settings import Settings, get_settings


def test_settings_load_from_env() -> None:
    """Проверяет корректность загрузки настроек и разбора путей."""
    settings = get_settings()
    assert settings.app_access_token == "Master"
    assert settings.app_results_limit == 50
    assert "ГАЛЕРЕЯ" in settings.exclude_keywords
    assert "Gallery" in settings.exclude_keywords
    assert settings.resolved_db_path.name == "pulsar.db"


def test_settings_validation_errors() -> None:
    """Проверяет возникновение ошибок валидации при неверных параметрах."""
    with pytest.raises(ValidationError):
        # Слишком короткий access token
        Settings(app_access_token="123")

    with pytest.raises(ValidationError):
        # Недопустимое дефолтное значение токена
        Settings(app_access_token="admin")

    with pytest.raises(ValidationError):
        # Слишком короткий session_secret_key
        Settings(session_secret_key="short-key")

    with pytest.raises(ValidationError):
        # results_limit больше допустимого
        Settings(app_results_limit=500)


def test_config_backwards_compatibility() -> None:
    """Проверяет, что функции в app/config.py возвращают корректные датаклассы."""
    app_s = config.get_app_settings()
    assert isinstance(app_s, config.AppSettings)
    assert app_s.access_token == "Master"
    assert isinstance(app_s.storage_dir, Path)

    sqlite_s = config.get_sqlite_settings()
    assert isinstance(sqlite_s, config.SQLiteSettings)
    assert sqlite_s.url == str(sqlite_s.db_path)

    gdrive_s = config.get_google_drive_settings()
    assert isinstance(gdrive_s, config.GoogleDriveSettings)
    assert isinstance(gdrive_s.scopes, tuple)
