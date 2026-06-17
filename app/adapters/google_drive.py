from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.config import GoogleDriveSettings
from app.ports import FileMetadata, FileStoragePort

logger = logging.getLogger("app.adapters.google_drive")


class DriveFile:
    def __init__(
        self,
        file_id: str,
        name: str,
        mime_type: str,
        size: str | None,
        md5_checksum: str | None = None,
        created_time: str | None = None,
        modified_time: str | None = None,
        parents: tuple[str, ...] = (),
    ) -> None:
        self.file_id = file_id
        self.name = name
        self.mime_type = mime_type
        self.size = size
        self.md5_checksum = md5_checksum
        self.created_time = created_time
        self.modified_time = modified_time
        self.parents = parents


class GoogleDriveAdapter(FileStoragePort):
    """Адаптер для взаимодействия с Google Drive API."""

    _cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _CACHE_TTL = 300  # 5 минут

    def __init__(self, settings: GoogleDriveSettings) -> None:
        self.settings = settings
        self._creds: service_account.Credentials | None = None

    def _get_creds(self) -> service_account.Credentials:
        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_file(
                str(self.settings.credentials_path), scopes=self.settings.scopes
            )
        return self._creds

    async def _get_access_token(self) -> str:
        creds = self._get_creds()
        if not creds.valid:
            # Обновление токена доступа в синхронном потоке (вызывается раз в час)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: creds.refresh(Request()))
        return str(creds.token)

    async def _authorized_get_json(self, url: str) -> dict[str, Any]:
        access_token = await self._get_access_token()
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30.0)
            response.raise_for_status()
            try:
                return dict(response.json())
            except json.JSONDecodeError as e:
                logger.error(
                    f"JSON decode error from {url}. Status: {response.status_code}, Body snippet: {response.text[:200]}"
                )
                raise e

    async def _list_files_page(
        self,
        *,
        page_size: int,
        page_token: str | None = None,
        query_filter: str | None = None,
        order_by: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "pageSize": page_size,
            "fields": (
                "nextPageToken,files(id,name,mimeType,size,md5Checksum,webViewLink,createdTime,modifiedTime,parents,owners,sharingUser)"
            ),
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        if query_filter:
            params["q"] = query_filter
        if order_by:
            params["orderBy"] = order_by
        query = urlencode(params)
        url = f"https://www.googleapis.com/drive/v3/files?{query}"
        try:
            return await self._authorized_get_json(url)
        except Exception as e:
            logger.error(f"Google Drive API error at {url}: {e}")
            raise

    def _to_drive_files(self, files: list[dict[str, Any]]) -> list[DriveFile]:
        return [
            DriveFile(
                file_id=item["id"],
                name=item["name"],
                mime_type=item["mimeType"],
                size=item.get("size"),
                md5_checksum=item.get("md5Checksum"),
                created_time=item.get("createdTime"),
                modified_time=item.get("modifiedTime"),
                parents=tuple(item.get("parents", [])),
            )
            for item in files
        ]

    async def list_files(self, page_size: int = 10) -> list[DriveFile]:
        response = await self._list_files_page(page_size=page_size)
        return self._to_drive_files(response.get("files", []))

    async def get_file(self, file_id: str) -> DriveFile:
        query = urlencode(
            {
                "fields": "id,name,mimeType,size,md5Checksum,createdTime,modifiedTime,parents,owners,sharingUser",
                "supportsAllDrives": "true",
            }
        )
        response = await self._authorized_get_json(f"https://www.googleapis.com/drive/v3/files/{file_id}?{query}")
        return self._to_drive_files([response])[0]

    async def get_file_metadata(self, file_id: str) -> FileMetadata:
        file_info = await self.get_file(file_id)
        return FileMetadata(
            name=file_info.name,
            mime_type=file_info.mime_type,
            size=int(file_info.size or 0),
            md5_checksum=file_info.md5_checksum,
            parents=list(file_info.parents),
        )

    async def list_folder_files(
        self,
        folder_id: str,
        *,
        page_size: int = 100,
        order_by: str = "createdTime asc",
        mime_prefix: str | None = None,
        max_files: int | None = None,
    ) -> list[DriveFile]:
        query_parts = [
            f"'{folder_id}' in parents",
            "trashed = false",
        ]
        if mime_prefix:
            query_parts.append(f"mimeType contains '{mime_prefix}'")
        query_filter = " and ".join(query_parts)

        page_token: str | None = None
        collected: list[DriveFile] = []
        while True:
            response = await self._list_files_page(
                page_size=page_size,
                page_token=page_token,
                query_filter=query_filter,
                order_by=order_by,
            )
            collected.extend(self._to_drive_files(response.get("files", [])))
            if max_files is not None and len(collected) >= max_files:
                return collected[:max_files]
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return collected

    async def list_folder_contents(self, folder_id: str, use_cache: bool = True) -> list[dict[str, Any]]:
        if use_cache and folder_id in self._cache:
            ts, data = self._cache[folder_id]
            if time.time() - ts < self._CACHE_TTL:
                return data

        items: list[dict[str, Any]] = []
        seen_ids = set()

        async def fetch_shared_drives() -> list[dict[str, Any]]:
            res_items = []
            try:
                page_token = None
                while True:
                    sd_url = "https://www.googleapis.com/drive/v3/drives?pageSize=100"
                    if page_token:
                        sd_url += f"&pageToken={page_token}"
                    drives_res = await self._authorized_get_json(sd_url)
                    for d in drives_res.get("drives", []):
                        res_items.append(
                            {
                                "id": d["id"],
                                "name": d["name"],
                                "mime_type": "application/vnd.google-apps.folder",
                                "is_folder": True,
                                "owner": "Shared Drive",
                            }
                        )
                    page_token = drives_res.get("nextPageToken")
                    if not page_token:
                        break
            except Exception as e:
                logger.error(f"Error fetching shared drives: {e}")
            return res_items

        async def fetch_files(query: str) -> list[dict[str, Any]]:
            res_items = []
            page_token = None
            while True:
                response = await self._list_files_page(
                    page_size=1000, query_filter=query, order_by="name", page_token=page_token
                )
                for f in response.get("files", []):
                    owner_name = None
                    if f.get("sharingUser"):
                        owner_name = f["sharingUser"].get("displayName")
                    elif f.get("owners"):
                        owner_name = f["owners"][0].get("displayName")

                    res_items.append(
                        {
                            "id": f["id"],
                            "name": f["name"],
                            "mime_type": f["mimeType"],
                            "md5_checksum": f.get("md5Checksum"),
                            "web_view_link": f.get("webViewLink"),
                            "is_folder": f["mimeType"] == "application/vnd.google-apps.folder",
                            "owner": owner_name,
                        }
                    )
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return res_items

        if folder_id == "root":
            shared_query = "sharedWithMe = true and trashed = false and mimeType = 'application/vnd.google-apps.folder'"
            results = await asyncio.gather(
                fetch_shared_drives(),
                fetch_files(shared_query),
                return_exceptions=True,
            )

            for res in results:
                if isinstance(res, list):
                    for item in res:
                        if item["id"] not in seen_ids:
                            items.append(item)
                            seen_ids.add(item["id"])
                elif isinstance(res, Exception):
                    logger.error(f"Parallel fetch error: {res}")
        else:
            query_filter = (
                f"'{folder_id}' in parents and trashed = false and "
                "(mimeType = 'application/vnd.google-apps.folder' or mimeType contains 'video/')"
            )
            items = await fetch_files(query_filter)

        sorted_items = sorted(items, key=lambda x: (not x["is_folder"], str(x["name"]).lower()))
        self._cache[folder_id] = (time.time(), sorted_items)
        return sorted_items

    async def download_file(
        self,
        file_id: str,
        destination: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)

        meta = await self.get_file(file_id)
        total_size = int(meta.size) if meta.size else None

        access_token = await self._get_access_token()
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"

        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET", url, headers={"Authorization": f"Bearer {access_token}"}, timeout=None
            ) as response:
                response.raise_for_status()

                with destination.open("wb") as f:
                    downloaded = 0
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size:
                            progress_callback(downloaded, total_size)

    async def open_media_stream(self, file_id: str, *, range_header: str | None = None) -> httpx.Response:
        query = urlencode({"supportsAllDrives": "true"})
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&{query}"
        headers: dict[str, str] = {}
        if range_header:
            headers["Range"] = range_header

        access_token = await self._get_access_token()
        headers["Authorization"] = f"Bearer {access_token}"

        client = httpx.AsyncClient()
        try:
            request = client.build_request("GET", url, headers=headers)
            response = await client.send(request, stream=True)
            response._client = client  # type: ignore
            return response
        except Exception:
            await client.aclose()
            raise
