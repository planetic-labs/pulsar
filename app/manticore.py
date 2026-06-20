import hashlib
import logging
import os
import socket

import httpx

from app.config import get_manticore_settings

logger = logging.getLogger(__name__)

_client = None


class Record:
    def __init__(self, id: int, payload: dict) -> None:
        self.id = id
        self.payload = payload


class ScoredPoint:
    def __init__(self, id: int, score: float, payload: dict) -> None:
        self.id = id
        self.score = score
        self.payload = payload


def str_to_manticore_id(val: str | int) -> int:
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except ValueError:
        return int(hashlib.md5(val.encode("utf-8")).hexdigest()[:15], 16)


def date_to_int(date_str: str | None) -> int:
    if not date_str:
        return 0
    clean_date = date_str.strip()[:10]
    clean_date = clean_date.replace("-", "")
    try:
        return int(clean_date)
    except ValueError:
        return 0


def int_to_date(date_int: int | None) -> str | None:
    if not date_int:
        return None
    val_str = str(date_int)
    if len(val_str) != 8:
        return None
    return f"{val_str[:4]}-{val_str[4:6]}-{val_str[6:]}"


def escape_string(val: str) -> str:
    """Безопасно экранирует строковый литерал для использования в SQL-запросах Manticore."""
    if not isinstance(val, str):
        return str(val)
    escaped = val.replace("\\", "\\\\")
    escaped = escaped.replace("'", "''")
    escaped = escaped.replace("\0", "\\0")
    escaped = escaped.replace("\n", "\\n")
    escaped = escaped.replace("\r", "\\r")
    escaped = escaped.replace("\x1a", "\\Z")
    return escaped


class ManticoreClient:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        # Переиспользуем один клиент для экономии сетевых ресурсов
        self.http_client = httpx.Client(timeout=30.0)

    def _execute_sql(self, sql: str) -> dict:
        """Выполняет SELECT-запросы через HTTP-эндпоинт /sql."""
        r = self.http_client.post(f"{self.url}/sql?mode=raw", content=sql)
        r.raise_for_status()
        return r.json()

    def _execute_ddl(self, sql: str) -> str:
        """Выполняет CREATE/DROP запросы через обязательный эндпоинт /cli."""
        cleaned_sql = sql.strip()

        import time

        max_retries = 20
        for attempt in range(max_retries):
            try:
                r = self.http_client.post(
                    f"{self.url}/cli", content=cleaned_sql, headers={"Content-Type": "text/plain"}, timeout=15.0
                )
                if r.status_code == 501:
                    logger.warning(
                        f"Manticore Buddy is not ready yet (501 Not Implemented). "
                        f"Retrying in 1s... (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(1.0)
                    continue
                r.raise_for_status()

                # ВЫВОДИМ ВООБЩЕ ВСЁ В КОНСОЛЬ ДЛЯ ОTЛАДКИ
                logger.info("=" * 50)
                logger.info(f"Запрос к /cli: {cleaned_sql[:50]}...")
                logger.info(f"Ответ от /cli: {r.text.strip()}")
                logger.info("=" * 50)

                return r.text
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                if attempt == max_retries - 1:
                    raise
                logger.warning(f"DDL execution failed: {e}. Retrying in 1s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(1.0)

        raise RuntimeError("Failed to execute DDL: Max retries exceeded")

    def upsert(self, collection_name: str, points: list[dict]) -> None:
        import json

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

            # Добавляем вектор в документ для Manticore KNN
            if "vector" in point:
                if isinstance(point["vector"], dict) and "default" in point["vector"]:
                    doc["vec"] = point["vector"]["default"]
                elif isinstance(point["vector"], list):
                    doc["vec"] = point["vector"]

            line = {"replace": {"table": collection_name, "id": m_id, "doc": doc}}
            lines.append(json.dumps(line, ensure_ascii=False))

        body = "\n".join(lines) + "\n"
        r = self.http_client.post(
            f"{self.url}/json/bulk",
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            timeout=None,
        )
        if r.status_code >= 400:
            logger.error(f"Manticore bulk request failed. Status: {r.status_code}. Response: {r.text}")
        r.raise_for_status()

        # Проверяем внутренние ошибки Manticore внутри bulk ответа
        resp_data = r.json()
        if resp_data.get("errors"):
            logger.error(f"Manticore bulk upsert errors: {resp_data}")
            raise RuntimeError(f"Manticore bulk upsert failed: {resp_data.get('error', 'check logs')}")

    def delete(self, collection_name: str, ids: list[str | int] | None = None, where_clause: str | None = None) -> None:
        if ids:
            m_ids = [str_to_manticore_id(x) for x in ids]
            ids_str = ",".join(str(x) for x in m_ids)
            self._execute_ddl(f"DELETE FROM {collection_name} WHERE id IN ({ids_str})")
        elif where_clause:
            self._execute_ddl(f"DELETE FROM {collection_name} WHERE {where_clause}")

    def scroll(
        self, collection_name: str, where_clause: str | None = None, limit: int = 10
    ) -> tuple[list[Record], None]:
        sql = f"SELECT * FROM {collection_name}"
        if where_clause:
            sql += f" WHERE {where_clause}"
        sql += f" LIMIT {limit}"

        res = self._execute_sql(sql)

        records = []
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
                for k in ["is_short", "is_4k", "is_primary"]:
                    if k in row_dict:
                        row_dict[k] = bool(row_dict[k])
                records.append(Record(id=m_id, payload=row_dict))

        return records, None

    def query_points(
        self,
        collection_name: str,
        query: list[float] | str,
        using: str = "default",
        where_clause: str | None = None,
        limit: int = 20,
    ) -> list[ScoredPoint]:
        if using == "default":
            vec_str = ",".join(map(str, query))
            sql = f"SELECT *, knn_dist() as dist FROM {collection_name} WHERE knn(vec, {limit}, ({vec_str}))"
            if where_clause:
                sql += f" AND {where_clause}"
        else:
            if isinstance(query, str):
                # Ограничиваем длину и символы в поисковых запросах согласно рекомендации безопасности C-04
                clean_query = query[:256]
                clean_query = "".join(ch for ch in clean_query if ch.isprintable() or ch in ("\n", "\r"))
                escaped_query = escape_string(clean_query)
                snippet_sql = (
                    f", SNIPPET(text, '{escaped_query}', "
                    f"'limit=10000', 'around=1000', "
                    f"'before_match=<mark>', 'after_match=</mark>') as highlighted_text"
                )
                if using and using != "default":
                    sql = (
                        f"SELECT *, weight() as w {snippet_sql} "
                        f"FROM {collection_name} WHERE MATCH('@{using} {escaped_query}')"
                    )
                else:
                    sql = f"SELECT *, weight() as w {snippet_sql} FROM {collection_name} WHERE MATCH('{escaped_query}')"
                if where_clause:
                    sql += f" AND {where_clause}"
                sql += f" LIMIT {limit}"
            else:
                sql = f"SELECT * FROM {collection_name}"
                if where_clause:
                    sql += f" WHERE {where_clause}"
                sql += f" LIMIT {limit}"

        logger.info(f"Executing query_points SQL: {sql[:150]}... (total length: {len(sql)})")
        try:
            res = self._execute_sql(sql)
        except Exception as e:
            logger.error(f"Manticore query failed: {e}. SQL: {sql}")
            return []
        points = []
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

                points.append(ScoredPoint(id=m_id, score=score, payload=row_dict))

        return points

    def retrieve(self, collection_name: str, ids: list[str | int]) -> list[Record]:
        if not ids:
            return []
        m_ids = [str_to_manticore_id(x) for x in ids]
        ids_str = ",".join(str(x) for x in m_ids)
        sql = f"SELECT * FROM {collection_name} WHERE id IN ({ids_str}) LIMIT {len(ids)}"
        res = self._execute_sql(sql)
        records = []
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
                for k in ["is_short", "is_4k", "is_primary"]:
                    if k in row_dict:
                        row_dict[k] = bool(row_dict[k])
                records.append(Record(id=m_id, payload=row_dict))
        return records

    def delete_collection(self, collection_name: str) -> None:
        """Удаляет таблицу через проверенный эндпоинт /cli."""
        self._execute_ddl(f"DROP TABLE IF EXISTS {collection_name}")


def get_manticore_client() -> ManticoreClient:
    global _client
    if _client is None:
        settings = get_manticore_settings()
        url = settings.url
        if "manticore" in url and not os.getenv("DOCKER_CONTAINER"):
            try:
                socket.gethostbyname("manticore")
            except socket.gaierror:
                logger.info("Host 'manticore' not found, falling back to localhost:9308")
                url = url.replace("manticore", "localhost")
        _client = ManticoreClient(url=url)
    return _client


def init_manticore() -> None:
    client = get_manticore_client()
    settings = get_manticore_settings()
    table_name = settings.table_name

    from app.config import get_embedding_settings

    emb_settings = get_embedding_settings()
    vector_size = emb_settings.dimension or 1024

    sql_chunks = f"""
    CREATE TABLE IF NOT EXISTS `{table_name}` (
        `text` text,
        `title` text,
        `chunk_id` bigint,
        `video_id` string,
        `source_file_id` string,
        `source_url` string,
        `recorded_date` int,
        `chunk_index` int,
        `start_sec` float,
        `end_sec` float,
        `is_short` int,
        `is_4k` int,
        `is_primary` int,
        `vec` float_vector knn_type='hnsw' knn_dims='{vector_size}' hnsw_similarity='cosine' quantization='1bit'
    ) type='rt' rt_mem_limit='512M' morphology='stem_ru'
    """

    logger.info(f"Sending DDL for {table_name} via /cli...")
    client._execute_ddl(sql_chunks)

    # Заставляем Manticore принудительно обновить таблицы в памяти
    logger.info("Flushing Manticore metadata...")
    client._execute_ddl("FLUSH TABLES")

    logger.info("Manticore tables successfully initialized.")


class SparseVector:
    def __init__(self, indices: list[int], values: list[float]) -> None:
        self.indices = indices
        self.values = values


class ModelsNamespace:
    SparseVector = SparseVector
    ScoredPoint = ScoredPoint
    Record = Record


models = ModelsNamespace()
