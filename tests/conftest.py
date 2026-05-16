import os
import sqlite3
import tempfile
from pathlib import Path

# --- PRE-IMPORT SETUP ---
_tmp_dir = Path(tempfile.gettempdir()) / "pulsar_tests"
_tmp_dir.mkdir(parents=True, exist_ok=True)
_db_file = _tmp_dir / "test_search_ui_global.db"

os.environ["APP_ACCESS_TOKEN"] = "test-token"
os.environ["SESSION_SECRET_KEY"] = "test-secret"
os.environ["QDRANT_URL"] = "http://mock-qdrant:6333"
os.environ["SQLITE_DB_PATH"] = str(_db_file)
os.environ["APP_DATA_DIR"] = str(_tmp_dir)
os.environ["APP_STORAGE_DIR"] = str(_tmp_dir / "storage")

import pytest
from fastapi.testclient import TestClient

from app.config import SQLiteSettings
from app.db import db_connection, init_db
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def test_env():
    """Ensure global test database is initialized."""
    db_path = os.environ["SQLITE_DB_PATH"]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)
    yield


@pytest.fixture(scope="function")
def tmp_db(tmp_path):
    """Create a temporary initialized SQLite database."""
    db_file = tmp_path / "test_search_ui.db"
    settings = SQLiteSettings(db_path=db_file)

    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        init_db(conn)

    return settings


@pytest.fixture(scope="function")
def mock_db_conn(tmp_db):
    """Fixture for database connection using tmp_db."""
    with db_connection(tmp_db) as conn:
        yield conn


@pytest.fixture(scope="function")
def client(tmp_db, monkeypatch):
    """FastAPI test client with injected test database."""
    monkeypatch.setattr("app.main.get_sqlite_settings", lambda: tmp_db)
    monkeypatch.setattr("app.main.init_qdrant", lambda: None)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def mock_qdrant(mocker):
    """Mock for Qdrant client."""
    mock = mocker.patch("app.qdrant.QdrantClient", autospec=True)
    return mock.return_value


@pytest.fixture(scope="function")
def mock_embedding_client(mocker):
    """Mock for UnifiedEmbeddingClient."""
    mock = mocker.patch("app.gemini.UnifiedEmbeddingClient", autospec=True)
    return mock.return_value
