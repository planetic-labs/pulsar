from app.database import Database


class UserSettingsRepository:
    """Persists preferences scoped to an authenticated user."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def is_search_history_enabled(self, user_id: str) -> bool:
        sql = "SELECT search_history_enabled FROM user_settings WHERE user_id = ?;"
        async with self.db.transaction() as conn, conn.execute(sql, (user_id,)) as cursor:
            row = await cursor.fetchone()
        return bool(row["search_history_enabled"]) if row else False

    async def set_search_history_enabled(self, user_id: str, enabled: bool) -> None:
        sql = """
            INSERT INTO user_settings (user_id, search_history_enabled)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE
            SET search_history_enabled = excluded.search_history_enabled;
        """
        async with self.db.transaction() as conn:
            await conn.execute(sql, (user_id, enabled))
