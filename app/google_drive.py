from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request

from app.config import GoogleDriveSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriveFile:
    file_id: str
    name: str
    mime_type: str
    size: str | None
    created_time: str | None = None
    modified_time: str | None = None
    parents: tuple[str, ...] = ()


class GoogleDriveClient:
    _cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _CACHE_TTL = 300  # 5 minutes

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
            # Refreshing credentials requires a transport. 
            # We use a synchronous request here because it's only once an hour.
            creds.refresh(Request())
        return str(creds.token)

    async def _authorized_get_json(self, url: str) -> dict[str, Any]:
        access_token = await self._get_access_token()
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=30.0)
            response.raise_for_status()
            try:
                return dict(response.json())
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error from {url}. Status: {response.status_code}, Body snippet: {response.text[:200]}")
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
            "fields": ("nextPageToken,files(id,name,mimeType,size,webViewLink,createdTime,modifiedTime,parents)"),
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
                "fields": "id,name,mimeType,size,createdTime,modifiedTime,parents",
                "supportsAllDrives": "true",
            }
        )
        response = await self._authorized_get_json(f"https://www.googleapis.com/drive/v3/files/{file_id}?{query}")
        return self._to_drive_files([response])[0]

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
        """Lists folders and videos. Parallel fetching for root and caching enabled."""
        if use_cache and folder_id in self._cache:
            ts, data = self._cache[folder_id]
            if time.time() - ts < self._CACHE_TTL:
                return data

        items: list[dict[str, Any]] = []
        seen_ids = set()

        async def fetch_shared_drives():
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
                                "name": f"D: {d['name']}",
                                "mime_type": "application/vnd.google-apps.folder",
                                "is_folder": True,
                            }
                        )
                    page_token = drives_res.get("nextPageToken")
                    if not page_token:
                        break
            except Exception as e:
                logger.error(f"Error fetching shared drives: {e}")
            return res_items

        async def fetch_files(query: str, prefix: str = ""):
            res_items = []
            page_token = None
            while True:
                response = await self._list_files_page(
                    page_size=1000, query_filter=query, order_by="name", page_token=page_token
                )
                for f in response.get("files", []):
                    res_items.append(
                        {
                            "id": f["id"],
                            "name": f"{prefix}{f['name']}",
                            "mime_type": f["mimeType"],
                            "web_view_link": f.get("webViewLink"),
                            "is_folder": f["mimeType"] == "application/vnd.google-apps.folder",
                        }
                    )
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return res_items

        if folder_id == "root":
            # Parallel fetch for root: Shared Drives + My Drive + Shared With Me
            my_drive_query = (
                "trashed = false and (mimeType = 'application/vnd.google-apps.folder' or mimeType contains 'video/')"
            )
            shared_query = (
                "sharedWithMe = true and trashed = false and "
                "(mimeType = 'application/vnd.google-apps.folder' or mimeType contains 'video/')"
            )

            results = await asyncio.gather(
                fetch_shared_drives(),
                fetch_files(my_drive_query),
                fetch_files(shared_query, prefix="S: "),
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
            # Inside a folder, we MUST filter by parents
            query_filter = (
                f"'{folder_id}' in parents and trashed = false and "
                "(mimeType = 'application/vnd.google-apps.folder' or mimeType contains 'video/')"
            )
            items = await fetch_files(query_filter)

        # Sort folders first, then by name
        sorted_items = sorted(items, key=lambda x: (not x["is_folder"], str(x["name"]).lower()))

        # Update cache
        self._cache[folder_id] = (time.time(), sorted_items)
        return sorted_items

    async def download_file(
        self,
        file_id: str,
        destination: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
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

        return destination

    async def open_media_stream(self, file_id: str, *, range_header: str | None = None):
        """Returns an httpx.Response object for streaming."""
        query = urlencode({"supportsAllDrives": "true"})
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&{query}"
        headers: dict[str, str] = {}
        if range_header:
            headers["Range"] = range_header

        access_token = await self._get_access_token()
        headers["Authorization"] = f"Bearer {access_token}"

        client = httpx.AsyncClient()
        try:
            # We use send instead of stream context manager here
            # to return the response and allow the caller to close it.
            # The client should be closed when the response is closed.
            request = client.build_request("GET", url, headers=headers)
            response = await client.send(request, stream=True)
            # Attach client to response for cleanup
            response._client = client  # type: ignore
            return response
        except Exception:
            await client.aclose()
            raise
