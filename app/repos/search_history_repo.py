import time

from app.database import Database


class SearchHistoryRepository:
    """Репозиторий для управления историей поисковых запросов пользователей в SQLite."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add_query(self, user_id: str, query: str, max_items: int = 20) -> None:
        """Сохраняет или обновляет время поискового запроса в истории пользователя."""
        cleaned_query = query.strip()
        if not cleaned_query:
            return

        now = time.time()
        sql = """
            INSERT INTO search_history (user_id, query, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, query) DO UPDATE SET created_at = excluded.created_at;
        """
        trim_sql = """
            DELETE FROM search_history
            WHERE user_id = ?
              AND id NOT IN (
                  SELECT id FROM search_history
                  WHERE user_id = ?
                  ORDER BY created_at DESC, id DESC
                  LIMIT ?
              );
        """
        async with self.db.transaction() as conn:
            await conn.execute(sql, (user_id, cleaned_query, now))
            await conn.execute(trim_sql, (user_id, user_id, max_items))

    async def get_history(self, user_id: str, limit: int = 10) -> list[str]:
        """Возвращает список последних поисковых запросов пользователя."""
        sql = """
            SELECT query FROM search_history
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?;
        """
        async with self.db.transaction() as conn, conn.execute(sql, (user_id, limit)) as cursor:
            rows = await cursor.fetchall()
            return [str(row["query"]) for row in rows]

    async def delete_query(self, user_id: str, query: str) -> None:
        """Удаляет конкретный поисковый запрос из истории пользователя."""
        sql = "DELETE FROM search_history WHERE user_id = ? AND query = ?;"
        async with self.db.transaction() as conn:
            await conn.execute(sql, (user_id, query.strip()))

    async def clear_history(self, user_id: str) -> None:
        """Очищает всю историю поисковых запросов пользователя."""
        sql = "DELETE FROM search_history WHERE user_id = ?;"
        async with self.db.transaction() as conn:
            await conn.execute(sql, (user_id,))
