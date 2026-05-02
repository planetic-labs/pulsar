import os
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db, db_connection
from app.config import SQLiteSettings

@pytest.fixture(scope="session", autouse=True)
def test_env():
    """Set up test environment variables."""
    os.environ["APP_ACCESS_TOKEN"] = "test-token"
    os.environ["SESSION_SECRET_KEY"] = "test-secret"
    os.environ["QDRANT_URL"] = "http://mock-qdrant:6333"
    yield

@pytest.fixture(scope="function")
def tmp_db(tmp_path):
    """Create a temporary initialized SQLite database."""
    db_file = tmp_path / "test_search_ui.db"
    settings = SQLiteSettings(db_path=str(db_file))
    
    # Initialize schema
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
    # Monkeypatch get_sqlite_settings to return our tmp_db settings
    from app.main import get_sqlite_settings
    monkeypatch.setattr("app.main.get_sqlite_settings", lambda: tmp_db)
    
    # Prevent Qdrant initialization during test startup
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
