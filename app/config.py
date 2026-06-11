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
    cache_lru_size: int = 20
    provider: str = "custom"
    openrouter_providers: list[str] | None = None


@dataclass(frozen=True)
class LocalAISettings:
    embedding_model: str
    embedding_dimension: int


@dataclass(frozen=True)
class VoiceSettings:
    voice_api_token: str
    voice_api_url: str


@dataclass(frozen=True)
class ManticoreSettings:
    url: str
    table_name: str = "chunks"


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
    disk_space_buffer_gb: int
    max_audio_size_mb: int = 20
    ark_jwks_url: str | None = None
    ark_webhook_secret: str | None = None
    exclude_keywords: tuple[str, ...] = ()

    @property
    def raw_transcripts_dir(self) -> Path:
        return self.storage_dir / "transcripts" / "raw"

    def get_raw_transcript_path(self, source_file_id: str) -> Path:
        prefix = source_file_id[:2] if len(source_file_id) >= 2 else source_file_id
        return self.raw_transcripts_dir / prefix / f"{source_file_id}.json.gz"

    @property
    def normalized_transcripts_dir(self) -> Path:
        return self.storage_dir / "transcripts" / "normalized"

    def get_normalized_transcript_path(self, source_file_id: str) -> Path:
        prefix = source_file_id[:2] if len(source_file_id) >= 2 else source_file_id
        return self.normalized_transcripts_dir / prefix / f"{source_file_id}.json.gz"

    @property
    def downloads_dir(self) -> Path:
        return self.storage_dir / "downloads"

    @property
    def audio_dir(self) -> Path:
        return self.storage_dir / "audio"

    @property
    def voice_samples_dir(self) -> Path:
        return self.storage_dir / "voice_samples"

    def resolve_path(self, path: str | Path | None) -> Path | None:
        if not path:
            return None
        p = Path(path)
        if p.is_absolute():
            return p
        return self.storage_dir / p


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_sqlite_settings() -> SQLiteSettings:
    app_settings = get_app_settings()
    default_db = app_settings.data_dir / "pulsar.db"
    return SQLiteSettings(db_path=Path(os.getenv("SQLITE_DB_PATH", str(default_db))))


def get_embedding_settings() -> EmbeddingSettings:
    providers_raw = os.getenv("EMBEDDING_OPENROUTER_PROVIDERS")
    openrouter_providers = [p.strip() for p in providers_raw.split(",")] if providers_raw else None
    return EmbeddingSettings(
        api_url=os.getenv("EMBEDDING_API_URL", ""),
        api_token=os.getenv("EMBEDDING_API_TOKEN", ""),
        model_id=os.getenv("EMBEDDING_MODEL_ID", "BAAI/bge-m3"),
        dimension=int(os.getenv("EMBEDDING_DIMENSION", "1024")),
        cache_lru_size=int(os.getenv("EMBEDDING_CACHE_LRU_SIZE", "20")),
        provider=os.getenv("EMBEDDING_PROVIDER", "custom"),
        openrouter_providers=openrouter_providers,
    )


def get_manticore_settings() -> ManticoreSettings:
    return ManticoreSettings(
        url=os.getenv("MANTICORE_URL", "http://manticore:9308"), table_name=os.getenv("MANTICORE_TABLE", "chunks")
    )


def get_local_ai_settings() -> LocalAISettings:
    return LocalAISettings(
        embedding_model=os.getenv("LOCAL_EMBEDDING_MODEL", "intfloat/multilingual-e5-small"),
        embedding_dimension=int(os.getenv("LOCAL_EMBEDDING_DIMENSION", "384")),
    )


def get_google_drive_settings() -> GoogleDriveSettings:
    scopes_raw = os.getenv("GOOGLE_DRIVE_SCOPES", "https://www.googleapis.com/auth/drive.readonly")
    return GoogleDriveSettings(
        credentials_path=Path(os.getenv("GOOGLE_DRIVE_CREDENTIALS_PATH", "/app/config/service-key.json")),
        download_dir=Path(os.getenv("GOOGLE_DRIVE_DOWNLOAD_DIR", "/app/downloads")),
        scopes=tuple(s.strip() for s in scopes_raw.split(",") if s.strip()),
    )


def get_deepgram_settings() -> DeepgramSettings:
    return DeepgramSettings(
        api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        project_id=os.getenv("DEEPGRAM_PROJECT_ID", ""),
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
        access_token=os.getenv("APP_ACCESS_TOKEN", "change-me"),
        session_secret_key=os.getenv("SESSION_SECRET_KEY", "change-me-to-something-very-secret"),
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        results_limit=int(os.getenv("APP_RESULTS_LIMIT", "20")),
        download_concurrency=int(os.getenv("INGEST_DOWNLOAD_CONCURRENCY", "1")),
        process_concurrency=int(os.getenv("INGEST_PROCESS_CONCURRENCY", "1")),
        storage_dir=Path(os.getenv("APP_STORAGE_DIR", "/app/storage")),
        data_dir=Path(os.getenv("APP_DATA_DIR", "/app/data")),
        disk_space_buffer_gb=int(os.getenv("DISK_SPACE_BUFFER_GB", "3")),
        max_audio_size_mb=int(os.getenv("MAX_AUDIO_SIZE_MB", "20")),
        ark_jwks_url=os.getenv("ARK_JWKS_URL"),
        ark_webhook_secret=os.getenv("ARK_WEBHOOK_SECRET"),
        exclude_keywords=tuple(k.strip() for k in os.getenv("EXCLUDE_KEYWORDS", "").split(",") if k.strip()),
    )


def get_voice_settings() -> VoiceSettings:
    return VoiceSettings(voice_api_token=os.getenv("VOICE_API_TOKEN", ""), voice_api_url=os.getenv("VOICE_API_URL", ""))
