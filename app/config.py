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
class SQLiteSettings:
    db_path: Path

    @property
    def url(self) -> str:
        return str(self.db_path)


@dataclass(frozen=True)
class GoogleDriveSettings:
    credentials_path: Path
    token_path: Path
    download_dir: Path
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class DeepgramSettings:
    api_key: str
    project_id: str
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
class EmbeddingSettings:
    api_url: str
    api_token: str
    model_id: str = "BAAI/bge-m3"
    dimension: int = 1024


@dataclass(frozen=True)
class LocalAISettings:
    embedding_model: str
    embedding_dimension: int


@dataclass(frozen=True)
class VoiceSettings:
    voice_api_token: str
    voice_api_url: str


@dataclass(frozen=True)
class QdrantSettings:
    url: str
    collection_name: str = "chunks_m3"


@dataclass(frozen=True)
class AppSettings:
    access_token: str
    session_secret_key: str
    host: str
    port: int
    results_limit: int
    download_concurrency: int
    process_concurrency: int
    storage_dir: Path
    data_dir: Path

    @property
    def raw_transcripts_dir(self) -> Path:
        return self.storage_dir / "transcripts" / "raw"

    @property
    def normalized_transcripts_dir(self) -> Path:
        return self.storage_dir / "transcripts" / "normalized"

    @property
    def downloads_dir(self) -> Path:
        return self.storage_dir / "downloads"

    @property
    def audio_dir(self) -> Path:
        return self.storage_dir / "audio"

    @property
    def voice_samples_dir(self) -> Path:
        return self.storage_dir / "voice_samples"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_sqlite_settings() -> SQLiteSettings:
    app_settings = get_app_settings()
    default_db = app_settings.data_dir / "search_ui.db"
    return SQLiteSettings(db_path=Path(os.getenv("SQLITE_DB_PATH", str(default_db))))


def get_embedding_settings() -> EmbeddingSettings:
    return EmbeddingSettings(api_url=os.getenv("EMBEDDING_API_URL", ""), api_token=os.getenv("EMBEDDING_API_TOKEN", ""))


def get_qdrant_settings() -> QdrantSettings:
    return QdrantSettings(
        url=os.getenv("QDRANT_URL", "http://qdrant:6333"), collection_name=os.getenv("QDRANT_COLLECTION", "chunks_m3")
    )


def get_local_ai_settings() -> LocalAISettings:
    return LocalAISettings(
        embedding_model=os.getenv("LOCAL_EMBEDDING_MODEL", "intfloat/multilingual-e5-small"),
        embedding_dimension=int(os.getenv("LOCAL_EMBEDDING_DIMENSION", "384")),
    )


def get_google_drive_settings() -> GoogleDriveSettings:
    scopes_raw = os.getenv("GOOGLE_DRIVE_SCOPES", "https://www.googleapis.com/auth/drive.readonly")
    return GoogleDriveSettings(
        credentials_path=Path(os.getenv("GOOGLE_DRIVE_CREDENTIALS_PATH", "/srv/search-ui/config/google.json")),
        token_path=Path(os.getenv("GOOGLE_DRIVE_TOKEN_PATH", "/srv/search-ui/config/token.json")),
        download_dir=Path(os.getenv("GOOGLE_DRIVE_DOWNLOAD_DIR", "/srv/search-ui/downloads")),
        scopes=tuple(s.strip() for s in scopes_raw.split(",") if s.strip()),
    )


def get_deepgram_settings() -> DeepgramSettings:
    return DeepgramSettings(
        api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        project_id=os.getenv("DEEPGRAM_PROJECT_ID", "bfdbbda9-97c5-4d05-917d-1da52417adeb"),
        model=os.getenv("DEEPGRAM_MODEL", "nova-3"),
        language=os.getenv("DEEPGRAM_LANGUAGE", "ru"),
        smart_format=_env_bool("DEEPGRAM_SMART_FORMAT", True),
        punctuate=_env_bool("DEEPGRAM_PUNCTUATE", True),
        utterances=_env_bool("DEEPGRAM_UTTERANCES", True),
        paragraphs=_env_bool("DEEPGRAM_PARAGRAPHS", True),
        diarize=_env_bool("DEEPGRAM_DIARIZE", True),
        filler_words=_env_bool("DEEPGRAM_FILLER_WORDS", False),
        base_url=os.getenv("DEEPGRAM_BASE_URL", "https://api.deepgram.com/v1/listen"),
    )


def get_app_settings() -> AppSettings:
    return AppSettings(
        access_token=os.getenv("APP_ACCESS_TOKEN", "Master"),
        session_secret_key=os.getenv("SESSION_SECRET_KEY", "super-secret-key"),
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        results_limit=int(os.getenv("APP_RESULTS_LIMIT", "20")),
        download_concurrency=int(os.getenv("INGEST_DOWNLOAD_CONCURRENCY", "1")),
        process_concurrency=int(os.getenv("INGEST_PROCESS_CONCURRENCY", "1")),
        storage_dir=Path(os.getenv("APP_STORAGE_DIR", "/srv/search-ui/storage")),
        data_dir=Path(os.getenv("APP_DATA_DIR", "/srv/search-ui/data")),
    )


def get_voice_settings() -> VoiceSettings:
    return VoiceSettings(voice_api_token=os.getenv("VOICE_API_TOKEN", ""), voice_api_url=os.getenv("VOICE_API_URL", ""))
