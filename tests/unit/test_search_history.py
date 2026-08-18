from __future__ import annotations

from pathlib import Path

import pytest

from app.database import Database
from app.repos.search_history_repo import SearchHistoryRepository


@pytest.mark.asyncio
async def test_search_history_lifecycle(tmp_path: Path):
    db_path = tmp_path / "test_history.db"
    db = Database(db_path)
    await db.connect()
    await db.init_schema()

    repo = SearchHistoryRepository(db)

    # 1. Initially empty
    assert await repo.get_history("user1") == []

    # 2. Add queries
    await repo.add_query("user1", "первый запрос")
    await repo.add_query("user1", "второй запрос")
    await repo.add_query("user1", "третий запрос")

    history = await repo.get_history("user1", limit=10)
    assert history == ["третий запрос", "второй запрос", "первый запрос"]

    # 3. Repeated query updates timestamp and moves to front
    await repo.add_query("user1", "первый запрос")
    history = await repo.get_history("user1", limit=10)
    assert history == ["первый запрос", "третий запрос", "второй запрос"]

    # 4. User isolation
    await repo.add_query("user2", "запрос пользователя 2")
    assert await repo.get_history("user2") == ["запрос пользователя 2"]
    assert "запрос пользователя 2" not in await repo.get_history("user1")

    # 5. Delete specific query
    await repo.delete_query("user1", "третий запрос")
    history = await repo.get_history("user1")
    assert history == ["первый запрос", "второй запрос"]

    # 6. Max items limit trimming
    for i in range(10):
        await repo.add_query("user3", f"query_{i}", max_items=5)
    u3_history = await repo.get_history("user3", limit=10)
    assert len(u3_history) == 5
    assert u3_history[0] == "query_9"

    # 7. Clear history
    await repo.clear_history("user1")
    assert await repo.get_history("user1") == []

    await db.close()


@pytest.mark.asyncio
async def test_search_history_api(tmp_path: Path):
    from starlette.requests import Request

    from app.config import get_app_settings
    from app.routers.ui import api_delete_search_history, api_get_search_history

    db_path = tmp_path / "test_api_history.db"
    db = Database(db_path)
    await db.connect()
    await db.init_schema()

    repo = SearchHistoryRepository(db)
    await repo.add_query("test_user", "запрос 1")
    await repo.add_query("test_user", "запрос 2")

    # 1. Personal user session (JWT with user_id)
    req_user = Request({"type": "http", "method": "GET", "path": "/api/search/history", "headers": []})
    req_user.state.user_id = "test_user"
    req_user.state.is_key_auth = False

    res = await api_get_search_history(req_user, limit=5, search_history_repo=repo, token="jwt-token")
    assert res == {"history": ["запрос 2", "запрос 1"]}

    # 2. Key-based authentication (shared access token) -> history must be empty!
    settings = get_app_settings()
    req_key = Request({"type": "http", "method": "GET", "path": "/api/search/history", "headers": []})
    req_key.state.user_id = "admin"
    req_key.state.is_key_auth = True

    res_key = await api_get_search_history(req_key, limit=5, search_history_repo=repo, token=settings.access_token)
    assert res_key == {"history": []}

    # 3. DELETE single for personal user
    del_res = await api_delete_search_history(req_user, q="запрос 1", search_history_repo=repo, token="jwt-token")
    assert del_res == {"status": "ok"}
    res2 = await api_get_search_history(req_user, limit=5, search_history_repo=repo, token="jwt-token")
    assert res2 == {"history": ["запрос 2"]}

    # 4. DELETE all for personal user
    del_all = await api_delete_search_history(req_user, q=None, search_history_repo=repo, token="jwt-token")
    assert del_all == {"status": "ok"}
    res3 = await api_get_search_history(req_user, limit=5, search_history_repo=repo, token="jwt-token")
    assert res3 == {"history": []}

    await db.close()
