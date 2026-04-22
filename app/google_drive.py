from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

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
    def __init__(self, settings: GoogleDriveSettings) -> None:
        self.settings = settings

    def _client_config(self) -> dict:
        payload = json.loads(self.settings.credentials_path.read_text(encoding="utf-8"))
        installed = payload.get("installed")
        if not installed:
            raise ValueError(
                "Expected OAuth installed-app credentials in google.json under 'installed'."
            )
        return installed

    def _token_payload(self) -> dict | None:
        if not self.settings.token_path.exists():
            return None
        return json.loads(self.settings.token_path.read_text(encoding="utf-8"))

    def _write_token_payload(self, payload: dict) -> None:
        self.settings.token_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _write_auth_session(self, payload: dict) -> Path:
        session_path = self.settings.token_path.with_suffix(".auth.json")
        session_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return session_path

    def _read_auth_session(self) -> dict:
        session_path = self.settings.token_path.with_suffix(".auth.json")
        if not session_path.exists():
            raise FileNotFoundError(
                f"Auth session file not found: {session_path}. Run auth_init() first."
            )
        return json.loads(session_path.read_text(encoding="utf-8"))

    def _post_form(self, url: str, data: dict) -> dict:
        body = urlencode(data).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as e:
            logger.error(f"HTTP Error during POST to {url}: {e.code} {e.reason}\nBody: {e.read().decode('utf-8')}")
            raise

    def _refresh_access_token(self, refresh_token: str) -> dict:
        config = self._client_config()
        token_response = self._post_form(
            config["token_uri"],
            {
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        token_response["refresh_token"] = refresh_token
        token_response["created_at"] = int(time.time())
        self._write_token_payload(token_response)
        return token_response

    def auth_init(self) -> tuple[str, Path]:
        config = self._client_config()
        code_verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(24)
        redirect_uri = config["redirect_uris"][0]

        auth_url = (
            f"{config['auth_uri']}?"
            + urlencode(
                {
                    "client_id": config["client_id"],
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": " ".join(self.settings.scopes),
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": state,
                    "code_challenge": code_verifier,
                    "code_challenge_method": "plain",
                }
            )
        )

        session_path = self._write_auth_session(
            {
                "state": state,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "created_at": int(time.time()),
            }
        )
        return auth_url, session_path

    def auth_exchange(self, callback_url: str) -> dict:
        config = self._client_config()
        session_payload = self._read_auth_session()

        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(callback_url)
        query = parse_qs(parsed.query)
        returned_state = query.get("state", [None])[0]
        code = query.get("code", [None])[0]
        error = query.get("error", [None])[0]

        if error:
            raise RuntimeError(f"Google OAuth returned error: {error}")
        if not code:
            raise ValueError("Callback URL does not contain OAuth code.")
        if returned_state != session_payload["state"]:
            raise ValueError("OAuth state mismatch.")

        token_response = self._post_form(
            config["token_uri"],
            {
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "redirect_uri": session_payload["redirect_uri"],
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": session_payload["code_verifier"],
            },
        )
        token_response["created_at"] = int(time.time())
        self._write_token_payload(token_response)
        return token_response

    def _get_access_token(self) -> str:
        token_payload = self._token_payload()
        if token_payload:
            created_at = int(token_payload.get("created_at", 0))
            expires_in = int(token_payload.get("expires_in", 0))
            expires_at = created_at + expires_in - 60

            if token_payload.get("access_token") and time.time() < expires_at:
                return token_payload["access_token"]

            if token_payload.get("refresh_token"):
                refreshed = self._refresh_access_token(token_payload["refresh_token"])
                return refreshed["access_token"]

        raise RuntimeError(
            "No valid Google token found. Run auth-init, authorize in browser, "
            "then complete auth-exchange with the returned callback URL."
        )

    def _authorized_get_json(self, url: str) -> dict:
        access_token = self._get_access_token()
        request = Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _authorized_request(self, url: str, headers: dict[str, str] | None = None) -> Request:
        request_headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        if headers:
            request_headers.update(headers)
        return Request(url, headers=request_headers)

    def _list_files_page(
        self,
        *,
        page_size: int,
        page_token: str | None = None,
        query_filter: str | None = None,
        order_by: str | None = None,
    ) -> dict:
        params = {
            "pageSize": page_size,
            "fields": (
                "nextPageToken,"
                "files(id,name,mimeType,size,createdTime,modifiedTime,parents)"
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
            return self._authorized_get_json(url)
        except Exception as e:
            logger.error(f"Google Drive API error at {url}: {e}")
            raise

    def _to_drive_files(self, files: list[dict]) -> list[DriveFile]:
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

    def list_files(self, page_size: int = 10) -> list[DriveFile]:
        response = self._list_files_page(page_size=page_size)
        return self._to_drive_files(response.get("files", []))

    def get_file(self, file_id: str) -> DriveFile:
        query = urlencode(
            {
                "fields": "id,name,mimeType,size,createdTime,modifiedTime,parents",
                "supportsAllDrives": "true",
            }
        )
        response = self._authorized_get_json(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?{query}"
        )
        return self._to_drive_files([response])[0]

    def list_folder_files(
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
            response = self._list_files_page(
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

    def list_folder_contents(self, folder_id: str) -> list[dict]:
        """Lists folders and videos. Minimal filtering to guarantee results."""
        items = []
        seen_ids = set()

        if folder_id == "root":
            # 1. Try to get Shared Drives separately
            try:
                sd_url = "https://www.googleapis.com/drive/v3/drives?pageSize=100"
                drives_res = self._authorized_get_json(sd_url)
                for d in drives_res.get("drives", []):
                    items.append({
                        "id": d["id"],
                        "name": f"D: {d['name']}",
                        "mime_type": "application/vnd.google-apps.folder",
                        "is_folder": True
                    })
                    seen_ids.add(d["id"])
            except Exception: pass

            # 2. Get EVERYTHING (Folders and Videos) that is NOT trashed
            # We don't use 'in parents' here to catch shared items and root items at once
            query_filter = "trashed = false and (mimeType = 'application/vnd.google-apps.folder' or mimeType contains 'video/')"
        else:
            # Inside a folder, we MUST filter by parents
            query_filter = f"'{folder_id}' in parents and trashed = false and (mimeType = 'application/vnd.google-apps.folder' or mimeType contains 'video/')"
            
        response = self._list_files_page(
            page_size=1000,
            query_filter=query_filter,
            order_by="name"
        )
        
        for f in response.get("files", []):
            if f["id"] not in seen_ids:
                items.append({
                    "id": f["id"],
                    "name": f["name"],
                    "mime_type": f["mimeType"],
                    "is_folder": f["mimeType"] == "application/vnd.google-apps.folder"
                })
                seen_ids.add(f["id"])

        # Sort folders first, then by name
        return sorted(items, key=lambda x: (not x["is_folder"], x["name"].lower()))

    def download_file(
        self,
        file_id: str,
        destination: Path,
        progress_callback: callable[[int, int], None] | None = None,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        query = urlencode({"supportsAllDrives": "true", "fields": "id,size"})
        
        meta = self.get_file(file_id)
        total_size = int(meta.size) if meta.size else None

        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
        request = self._authorized_request(url)
        
        with urlopen(request) as response, destination.open("wb") as file_handle:
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                file_handle.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total_size:
                    progress_callback(downloaded, total_size)

        return destination

    def open_media_stream(self, file_id: str, *, range_header: str | None = None):
        query = urlencode({"supportsAllDrives": "true"})
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&{query}"
        headers: dict[str, str] = {}
        if range_header:
            headers["Range"] = range_header
        request = self._authorized_request(url, headers=headers)
        return urlopen(request)
