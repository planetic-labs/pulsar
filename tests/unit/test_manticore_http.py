from unittest.mock import AsyncMock, Mock

import pytest

from app.adapters.manticore import ManticoreAdapter
from app.manticore import ManticoreClient


def test_sync_ddl_uses_sql_endpoint() -> None:
    client = ManticoreClient("http://manticore:9308")
    response = Mock(text='[{"total":0}]')
    response.raise_for_status = Mock()
    client.http_client.post = Mock(return_value=response)

    result = client._execute_ddl(" CREATE TABLE test (title text) ")

    assert result == response.text
    client.http_client.post.assert_called_once_with(
        "http://manticore:9308/sql?mode=raw",
        content="CREATE TABLE test (title text)",
        headers={"Content-Type": "text/plain"},
        timeout=15.0,
    )


@pytest.mark.asyncio
async def test_async_ddl_uses_sql_endpoint() -> None:
    adapter = ManticoreAdapter("http://manticore:9308")
    response = Mock(text='[{"total":0}]')
    response.raise_for_status = Mock()
    adapter._client.post = AsyncMock(return_value=response)

    result = await adapter._execute_ddl(" DELETE FROM chunks WHERE id = 1 ")

    assert result == response.text
    adapter._client.post.assert_awaited_once_with(
        "http://manticore:9308/sql?mode=raw",
        content="DELETE FROM chunks WHERE id = 1",
        headers={"Content-Type": "text/plain"},
        timeout=15.0,
    )
    await adapter.close()
