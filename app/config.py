from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

def _load_dotenv_file(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

# Initialize environment
_load_dotenv_file(Path(__file__).resolve().parents[1] / ".env")

@dataclass(frozen=True)
class PostgresSettings:
    url: str

    @property
    def host(self) -> str:
        # Quick hack to get host for display
        return self.url.split("@")[1].split(":")[0]

@dataclass(frozen=True)
class GoogleDriveSettings:
    credentials_path: Path
    token_path: Path
    download_dir: Path
    scopes: tuple[str, ...]

@dataclass(frozen=True)
class TranscriptionSettings:
    engine: str
    whisper_model: str
    whisper_device: str

@dataclass(frozen=True)
class DeepgramSettings:
    api_key: str
    model: str
    language: str
    smart_format: bool
    punctuate: bool
    utterances: bool
    paragraphs: bool
    diarize: bool
    filler_words: bool
    base_url: str

@dataclass(frozen=True)
class GoogleAISettings:
    api_key: str
    embedding_model: str = "models/gemini-embedding-2-preview"

@dataclass(frozen=True)
class LocalAISettings:
    embedding_model: str
    embedding_dimension: int

@dataclass(frozen=True)
class AppSettings:
    access_token: str
    host: str
    port: int
    results_limit: int
    download_concurrency: int
    process_concurrency: int
    storage_dir: Path

    @property
    def raw_transcripts_dir(self) -> Path:
        return self.storage_dir / "transcripts" / "raw"

    @property
    def normalized_transcripts_dir(self) -> Path:
        return self.storage_dir / "transcripts" / "normalized"

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

def get_postgres_settings() -> PostgresSettings:
    return PostgresSettings(url=os.getenv("POSTGRES_URL", "postgresql://devman:password@db:5432/search_ui"))

def get_transcription_settings() -> TranscriptionSettings:
    return TranscriptionSettings(
        engine=os.getenv("TRANSCRIPTION_ENGINE", "deepgram").lower(),
        whisper_model=os.getenv("WHISPER_MODEL", "large-v3"),
        whisper_device=os.getenv("WHISPER_DEVICE", "cpu")
    )

def get_google_ai_settings() -> GoogleAISettings:
    return GoogleAISettings(
        api_key=os.getenv("GOOGLE_AI_API_KEY", "")
    )

def get_local_ai_settings() -> LocalAISettings:
    return LocalAISettings(
        embedding_model=os.getenv("LOCAL_EMBEDDING_MODEL", "intfloat/multilingual-e5-small"),
        embedding_dimension=int(os.getenv("LOCAL_EMBEDDING_DIMENSION", "384"))
    )

def get_google_drive_settings() -> GoogleDriveSettings:
    scopes_raw = os.getenv("GOOGLE_DRIVE_SCOPES", "https://www.googleapis.com/auth/drive.readonly")
    return GoogleDriveSettings(
        credentials_path=Path(os.getenv("GOOGLE_DRIVE_CREDENTIALS_PATH", "/srv/search-ui/google.json")),
        token_path=Path(os.getenv("GOOGLE_DRIVE_TOKEN_PATH", "/srv/search-ui/token.json")),
        download_dir=Path(os.getenv("GOOGLE_DRIVE_DOWNLOAD_DIR", "/srv/search-ui/downloads")),
        scopes=tuple(s.strip() for s in scopes_raw.split(",") if s.strip())
    )

def get_deepgram_settings() -> DeepgramSettings:
    return DeepgramSettings(
        api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
        language=os.getenv("DEEPGRAM_LANGUAGE", "ru"),
        smart_format=_env_bool("DEEPGRAM_SMART_FORMAT", True),
        punctuate=_env_bool("DEEPGRAM_PUNCTUATE", True),
        utterances=_env_bool("DEEPGRAM_UTTERANCES", True),
        paragraphs=_env_bool("DEEPGRAM_PARAGRAPHS", True),
        diarize=_env_bool("DEEPGRAM_DIARIZE", False),
        filler_words=_env_bool("DEEPGRAM_FILLER_WORDS", False),
        base_url=os.getenv("DEEPGRAM_BASE_URL", "https://api.deepgram.com/v1/listen")
    )

def get_app_settings() -> AppSettings:
    return AppSettings(
        access_token=os.getenv("APP_ACCESS_TOKEN", "Master"),
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        results_limit=int(os.getenv("APP_RESULTS_LIMIT", "20")),
        download_concurrency=int(os.getenv("INGEST_DOWNLOAD_CONCURRENCY", "1")),
        process_concurrency=int(os.getenv("INGEST_PROCESS_CONCURRENCY", "2")),
        storage_dir=Path(os.getenv("APP_STORAGE_DIR", "/srv/search-ui/storage"))
    )
