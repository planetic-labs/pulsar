from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.config import GoogleDriveSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriveFile:
    file_id: str
    name: str
    mime_type: str
    size: str | None
    md5_checksum: str | None = None
    created_time: str | None = None
    modified_time: str | None = None
    parents: tuple[str, ...] = ()


class GoogleDriveClient:
    _cache: ClassVar[dict[str, tuple[float, list[dict[str, Any]]]]] = {}
    _CACHE_TTL = 300  # 5 minutes
    _BATCH_SIZE = 100
    _FILE_FIELDS = "id,name,mimeType,size,md5Checksum,createdTime,modifiedTime,parents,owners,sharingUser"

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
        except httpx.HTTPError as e:
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
                "fields": self._FILE_FIELDS,
                "supportsAllDrives": "true",
            }
        )
        response = await self._authorized_get_json(f"https://www.googleapis.com/drive/v3/files/{file_id}?{query}")
        return self._to_drive_files([response])[0]

    @staticmethod
    def _parse_batch_response(content_type: str, content: bytes, request_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Parse Google Drive's multipart batch response, retaining successful file metadata only."""
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + content
        )
        if not message.is_multipart():
            raise ValueError("Google Drive batch response is not multipart")

        files: dict[str, dict[str, Any]] = {}
        for part in message.iter_parts():
            content_id = part.get("Content-ID", "")
            match = re.search(r"(\d+)>?$", content_id)
            if not match:
                logger.warning("Google Drive batch response has no usable Content-ID: %s", content_id)
                continue
            request_index = int(match.group(1))
            if request_index >= len(request_ids):
                logger.warning("Google Drive batch response has unknown Content-ID: %s", content_id)
                continue

            raw_response = part.get_payload(decode=True) or b""
            if not isinstance(raw_response, bytes):
                logger.warning(
                    "Google Drive batch response contains a non-bytes payload for %s", request_ids[request_index]
                )
                continue
            header_bytes, separator, body = raw_response.partition(b"\r\n\r\n")
            if not separator:
                header_bytes, separator, body = raw_response.partition(b"\n\n")
            status_match = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})", header_bytes)
            if not status_match or int(status_match.group(1)) != 200:
                status_line = header_bytes.decode(errors="replace").splitlines()
                logger.warning(
                    "Google Drive batch metadata request failed for %s: %s",
                    request_ids[request_index],
                    status_line[0] if status_line else "empty response",
                )
                continue
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                logger.warning("Google Drive batch response contains invalid JSON for %s", request_ids[request_index])
                continue
            if isinstance(payload, dict):
                files[request_ids[request_index]] = payload
        return files

    async def _get_files_batch_page(self, file_ids: list[str]) -> dict[str, dict[str, Any]]:
        boundary = f"batch_{uuid4().hex}"
        query = urlencode({"fields": self._FILE_FIELDS, "supportsAllDrives": "true"})
        parts: list[str] = []
        for index, file_id in enumerate(file_ids):
            parts.extend(
                (
                    f"--{boundary}",
                    "Content-Type: application/http",
                    f"Content-ID: <request-{index}>",
                    "Content-Transfer-Encoding: binary",
                    "",
                    f"GET /drive/v3/files/{quote(file_id, safe='')}?{query} HTTP/1.1",
                    "",
                )
            )
        body = "\r\n".join([*parts, f"--{boundary}--", ""]).encode()
        access_token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": f"multipart/mixed; boundary={boundary}",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post("https://www.googleapis.com/batch/drive/v3", headers=headers, content=body)
        response.raise_for_status()
        return self._parse_batch_response(response.headers.get("content-type", ""), response.content, file_ids)

    async def get_files_batch(self, file_ids: list[str]) -> dict[str, DriveFile]:
        """Retrieve file metadata in Drive API batches of at most 100 requests."""
        unique_file_ids = list(dict.fromkeys(file_id for file_id in file_ids if file_id))
        files: dict[str, DriveFile] = {}
        for start in range(0, len(unique_file_ids), self._BATCH_SIZE):
            batch_ids = unique_file_ids[start : start + self._BATCH_SIZE]
            try:
                payloads = await self._get_files_batch_page(batch_ids)
            except httpx.HTTPError as exc:
                logger.error("Google Drive batch metadata request failed for %d files: %s", len(batch_ids), exc)
                continue
            for file_id, payload in payloads.items():
                try:
                    files[file_id] = self._to_drive_files([payload])[0]
                except (KeyError, TypeError) as exc:
                    logger.warning("Google Drive batch metadata is incomplete for %s: %s", file_id, exc)
        return files

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
            except httpx.HTTPError as e:
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
            # Parallel fetch for root: Shared Drives + Shared With Me (Folders only)
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

        async with (
            httpx.AsyncClient() as client,
            client.stream("GET", url, headers={"Authorization": f"Bearer {access_token}"}, timeout=None) as response,
        ):
            response.raise_for_status()

            with destination.open("wb") as f:
                downloaded = 0
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_size:
                        progress_callback(downloaded, total_size)

        return destination

    async def open_media_stream(self, file_id: str, *, range_header: str | None = None) -> httpx.Response:
        """Returns an httpx.Response object for streaming."""
        query = urlencode({"supportsAllDrives": "true"})
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&{query}"
        headers: dict[str, str] = {}
        if range_header:
            headers["Range"] = range_header

        access_token = await self._get_access_token()
        headers["Authorization"] = f"Bearer {access_token}"

        timeout = httpx.Timeout(timeout=300.0, connect=20.0, read=300.0, write=60.0, pool=60.0)
        client = httpx.AsyncClient(timeout=timeout)
        try:
            # We use send instead of stream context manager here
            # to return the response and allow the caller to close it.
            # The client should be closed when the response is closed.
            request = client.build_request("GET", url, headers=headers)
            response = await client.send(request, stream=True)
            # Attach client to response for cleanup
            response._client = client  # type: ignore
            return response
        except BaseException:
            await client.aclose()
            raise
