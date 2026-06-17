from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.settings import get_settings


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

    def resolve_path(self, path: str | Path | None) -> Path | None:
        if not path:
            return None
        p = Path(path)
        if p.is_absolute():
            return p
        return self.storage_dir / p


def get_sqlite_settings() -> SQLiteSettings:
    s = get_settings()
    return SQLiteSettings(db_path=s.resolved_db_path)


def get_embedding_settings() -> EmbeddingSettings:
    s = get_settings()
    return EmbeddingSettings(
        api_url=s.embedding_api_url,
        api_token=s.embedding_api_token,
        model_id=s.embedding_model_id,
        dimension=s.embedding_dimension,
        cache_lru_size=s.embedding_cache_lru_size,
        provider=s.embedding_provider,
        openrouter_providers=s.embedding_openrouter_providers,
    )


def get_manticore_settings() -> ManticoreSettings:
    s = get_settings()
    return ManticoreSettings(
        url=s.manticore_url,
        table_name=s.manticore_table,
    )


def get_local_ai_settings() -> LocalAISettings:
    s = get_settings()
    return LocalAISettings(
        embedding_model=s.local_embedding_model,
        embedding_dimension=s.local_embedding_dimension,
    )


def get_google_drive_settings() -> GoogleDriveSettings:
    s = get_settings()
    return GoogleDriveSettings(
        credentials_path=s.google_drive_credentials_path,
        download_dir=s.google_drive_download_dir,
        scopes=s.google_drive_scopes,
    )


def get_deepgram_settings() -> DeepgramSettings:
    s = get_settings()
    return DeepgramSettings(
        api_key=s.deepgram_api_key,
        project_id=s.deepgram_project_id,
        model=s.deepgram_model,
        language=s.deepgram_language,
        smart_format=s.deepgram_smart_format,
        punctuate=s.deepgram_punctuate,
        utterances=s.deepgram_utterances,
        paragraphs=s.deepgram_paragraphs,
        diarize=s.deepgram_diarize,
        filler_words=s.deepgram_filler_words,
        base_url=s.deepgram_base_url,
    )


def get_app_settings() -> AppSettings:
    s = get_settings()
    return AppSettings(
        access_token=s.app_access_token,
        session_secret_key=s.session_secret_key,
        host=s.app_host,
        port=s.app_port,
        results_limit=s.app_results_limit,
        download_concurrency=s.ingest_download_concurrency,
        process_concurrency=s.ingest_process_concurrency,
        storage_dir=s.app_storage_dir,
        data_dir=s.app_data_dir,
        disk_space_buffer_gb=s.disk_space_buffer_gb,
        max_audio_size_mb=s.max_audio_size_mb,
        ark_jwks_url=s.ark_jwks_url,
        ark_webhook_secret=s.ark_webhook_secret,
        exclude_keywords=s.exclude_keywords,
    )


def get_voice_settings() -> VoiceSettings:
    s = get_settings()
    return VoiceSettings(
        voice_api_token=s.voice_api_token,
        voice_api_url=s.voice_api_url,
    )
