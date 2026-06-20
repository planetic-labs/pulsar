from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypedDict

import httpx

from app.manticore import models


class ScoredPoint(TypedDict):
    id: int
    score: float
    payload: dict[str, str | int | float | bool | list[float] | None]


class VectorPoint(TypedDict):
    id: int
    vector: dict[str, list[float]]
    payload: dict[str, str | int | float | bool | list[float] | None]


class FileMetadata(TypedDict):
    name: str
    mime_type: str
    size: int
    md5_checksum: str | None
    parents: list[str]


class TranscriptionPort(Protocol):
    """Интерфейс для работы со службой STT (распознавание речи)."""

    async def transcribe(self, audio_path: Path, diarize: bool = True) -> dict[str, Any]:
        """Отправляет аудиофайл на распознавание и возвращает сырой JSON-ответ."""
        ...

    async def check_balance(self, threshold: float) -> tuple[bool, float]:
        """Проверяет баланс и возвращает (баланс_выше_порога, текущий_баланс)."""
        ...

    async def get_balance_async(self, force_refresh: bool = False) -> dict[str, Any]:
        """Возвращает баланс STT-провайдера."""
        ...


class VectorStorePort(Protocol):
    """Интерфейс для работы с векторной базой данных (Manticore Search и др.)."""

    async def upsert_points(self, table: str, points: list[dict[str, Any]]) -> None:
        """Вставляет или обновляет вектора в таблице."""
        ...

    async def search_vectors(
        self, table: str, vector: list[float], limit: int, where: str | None = None
    ) -> list[ScoredPoint]:
        """Поиск по векторному сходству (KNN)."""
        ...

    async def search_fulltext(self, table: str, query: str, limit: int, where: str | None = None) -> list[ScoredPoint]:
        """Полнотекстовый поиск."""
        ...

    async def delete_points(self, table: str, ids: list[int]) -> None:
        """Удаляет точки по их числовым идентификаторам."""
        ...

    async def delete_points_by_where(self, table: str, where_clause: str) -> None:
        """Удаляет точки по условию WHERE."""
        ...

    async def filter_only(self, table: str, where_clause: str, limit: int) -> list[ScoredPoint]:
        """Поиск только по условиям фильтрации без векторного или полнотекстового запроса."""
        ...


class FileStoragePort(Protocol):
    """Интерфейс для доступа к удаленному файловому хранилищу (Google Drive и др.)."""

    async def download_file(
        self, file_id: str, destination: Path, progress_callback: Callable[[int, int], None] | None = None
    ) -> None:
        """Скачивает файл с удаленного хранилища на локальный диск."""
        ...

    async def get_file_metadata(self, file_id: str) -> FileMetadata:
        """Возвращает метаданные файла."""
        ...

    async def open_media_stream(self, file_id: str, *, range_header: str | None = None) -> httpx.Response:
        """Открывает поток для чтения медиафайла."""
        ...


class EmbeddingPort(Protocol):
    """Интерфейс для работы с сервисом генерации эмбеддингов."""

    async def embed_text(
        self, text: str, task_type: str = "RETRIEVAL_QUERY"
    ) -> tuple[list[float], models.SparseVector | None]:
        """Генерирует dense и sparse вектора для текста."""
        ...

    async def embed_batch(
        self, texts: list[str], progress_callback: Callable[[int, int], None] | None = None
    ) -> list[tuple[list[float], models.SparseVector | None]]:
        """Генерирует dense и sparse вектора для пакета текстов."""
        ...

    async def close(self) -> None:
        """Закрывает ресурсы."""
        ...
