from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import httpx

from app.ports import ScoredPoint, VectorStorePort

logger = logging.getLogger("app.adapters.manticore")


def str_to_manticore_id(val: str | int) -> int:
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except ValueError:
        return int(hashlib.md5(val.encode("utf-8")).hexdigest()[:15], 16)


class ManticoreAdapter(VectorStorePort):
    """Адаптер для взаимодействия с Manticore Search по HTTP API."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        # Асинхронный клиент
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _execute_sql(self, sql: str) -> list[dict[str, Any]]:
        response = await self._client.post(f"{self.url}/sql?mode=raw", content=sql)
        response.raise_for_status()
        return list(response.json())

    async def _execute_ddl(self, sql: str) -> str:
        cleaned_sql = sql.strip()
        max_retries = 20
        import asyncio

        for attempt in range(max_retries):
            try:
                response = await self._client.post(
                    f"{self.url}/cli",
                    content=cleaned_sql,
                    headers={"Content-Type": "text/plain"},
                    timeout=15.0,
                )
                if response.status_code == 501:
                    logger.warning(
                        f"Manticore Buddy is not ready yet (501 Not Implemented). "
                        f"Retrying in 1s... (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(1.0)
                    continue
                response.raise_for_status()
                return response.text
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if attempt == max_retries - 1:
                    raise
                logger.warning(f"DDL execution failed: {e}. Retrying in 1s... (attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(1.0)

        raise RuntimeError("Failed to execute DDL: Max retries exceeded")

    async def upsert_points(self, table: str, points: list[dict[str, Any]]) -> None:
        if not points:
            return

        lines = []
        for point in points:
            m_id = str_to_manticore_id(point["id"])
            doc = {}
            payload = point.get("payload", {})
            for k, v in payload.items():
                if isinstance(v, bool):
                    doc[k] = 1 if v else 0
                else:
                    doc[k] = v

            if "vector" in point:
                if isinstance(point["vector"], dict) and "default" in point["vector"]:
                    doc["vec"] = point["vector"]["default"]
                elif isinstance(point["vector"], list):
                    doc["vec"] = point["vector"]

            line = {"replace": {"table": table, "id": m_id, "doc": doc}}
            lines.append(json.dumps(line, ensure_ascii=False))

        body = "\n".join(lines) + "\n"
        response = await self._client.post(
            f"{self.url}/json/bulk",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            timeout=None,
        )
        if response.status_code >= 400:
            logger.error(f"Manticore bulk request failed. Status: {response.status_code}. Response: {response.text}")
        response.raise_for_status()

        resp_data = response.json()
        if resp_data.get("errors"):
            logger.error(f"Manticore bulk upsert errors: {resp_data}")
            raise RuntimeError(f"Manticore bulk upsert failed: {resp_data.get('error', 'check logs')}")

    async def delete_points(self, table: str, ids: list[int]) -> None:
        if not ids:
            return
        m_ids = [str_to_manticore_id(x) for x in ids]
        ids_str = ",".join(str(x) for x in m_ids)
        await self._execute_ddl(f"DELETE FROM {table} WHERE id IN ({ids_str})")

    async def delete_points_by_where(self, table: str, where_clause: str) -> None:
        await self._execute_ddl(f"DELETE FROM {table} WHERE {where_clause}")

    async def search_vectors(
        self, table: str, vector: list[float], limit: int, where: str | None = None
    ) -> list[ScoredPoint]:
        vec_str = ",".join(map(str, vector))
        sql = f"SELECT *, knn_dist() as dist FROM {table} WHERE knn(vec, {limit}, ({vec_str}))"
        if where:
            sql += f" AND {where}"
        return await self._execute_query(sql)

    async def search_fulltext(self, table: str, query: str, limit: int, where: str | None = None) -> list[ScoredPoint]:
        escaped_query = query.replace("'", "''")
        snippet_sql = (
            f", SNIPPET(text, '{escaped_query}', "
            f"'limit=10000', 'around=1000', "
            f"'before_match=<mark>', 'after_match=</mark>') as highlighted_text"
        )
        sql = f"SELECT *, weight() as w {snippet_sql} FROM {table} WHERE MATCH('{escaped_query}')"
        if where:
            sql += f" AND {where}"
        sql += f" LIMIT {limit}"
        return await self._execute_query(sql)

    async def _execute_query(self, sql: str) -> list[ScoredPoint]:
        logger.info(f"Executing query SQL: {sql[:150]}... (total length: {len(sql)})")
        res = await self._execute_sql(sql)
        points: list[ScoredPoint] = []
        if res and len(res) > 0:
            cols_meta = res[0].get("columns")
            if isinstance(cols_meta, list):
                columns = [list(c.keys())[0] for c in cols_meta]
            else:
                columns = list(cols_meta.keys()) if cols_meta else []
            data = res[0].get("data", [])
            for row in data:
                if isinstance(row, dict):
                    row_dict = row.copy()
                else:
                    row_dict = dict(zip(columns, row, strict=False))
                m_id = row_dict.pop("id")

                if "dist" in row_dict:
                    score = 1.0 - float(row_dict.pop("dist"))
                elif "w" in row_dict:
                    score = float(row_dict.pop("w"))
                else:
                    score = 1.0

                for k in ["is_short", "is_4k", "is_primary"]:
                    if k in row_dict:
                        row_dict[k] = bool(row_dict[k])

                points.append(
                    ScoredPoint(
                        id=int(m_id),
                        score=score,
                        payload=row_dict,
                    )
                )
        return points
