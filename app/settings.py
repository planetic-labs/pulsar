from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IS_CONTAINER = Path("/.dockerenv").exists() or str(PROJECT_ROOT) == "/app"


def resolve_host_path(path: Path | str) -> Path:
    """Разрешает пути хоста при работе вне контейнера Docker."""
    p = Path(path)
    if not IS_CONTAINER and len(p.parts) > 1 and p.parts[0] == "/" and p.parts[1] == "app":
        return PROJECT_ROOT / Path(*p.parts[2:])
    return p


def validate_path(v: Any) -> Path:
    if isinstance(v, (str, Path)):
        return resolve_host_path(v)
    raise ValueError(f"Expected str or Path, got {type(v)}")


def validate_optional_path(v: Any) -> Path | None:
    if v is None or str(v).strip() == "":
        return None
    return validate_path(v)


def parse_comma_separated_tuple(v: Any) -> tuple[str, ...]:
    if isinstance(v, str):
        return tuple(k.strip() for k in v.split(",") if k.strip())
    if isinstance(v, (list, tuple)):
        return tuple(str(k).strip() for k in v if str(k).strip())
    return ()


def parse_comma_separated_list(v: Any) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        if not v.strip():
            return None
        return [k.strip() for k in v.split(",") if k.strip()]
    if isinstance(v, (list, tuple)):
        return [str(k).strip() for k in v if str(k).strip()]
    return None


class Settings(BaseSettings):
    """Единая конфигурация приложения с валидацией при старте."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Core / App settings ---
    app_storage_dir: Annotated[Path, BeforeValidator(validate_path)] = Field(
        default=Path("/app/storage"), validation_alias="APP_STORAGE_DIR"
    )
    app_data_dir: Annotated[Path, BeforeValidator(validate_path)] = Field(
        default=Path("/app/data"), validation_alias="APP_DATA_DIR"
    )
    app_access_token: str = Field(min_length=32, validation_alias="APP_ACCESS_TOKEN")
    session_secret_key: str = Field(
        min_length=32,
        validation_alias="SESSION_SECRET_KEY",
    )
    admin_role_name: str = Field(default="admin", validation_alias="ADMIN_ROLE_NAME")
    app_host: str = Field(default="0.0.0.0", validation_alias="APP_HOST")
    app_port: int = Field(default=8350, validation_alias="APP_PORT")
    app_results_limit: int = Field(default=20, ge=1, le=200, validation_alias="APP_RESULTS_LIMIT")
    ingest_download_concurrency: int = Field(default=1, ge=1, validation_alias="INGEST_DOWNLOAD_CONCURRENCY")
    ingest_process_concurrency: int = Field(default=1, ge=1, validation_alias="INGEST_PROCESS_CONCURRENCY")
    disk_space_buffer_gb: int = Field(default=3, ge=1, validation_alias="DISK_SPACE_BUFFER_GB")
    max_audio_size_mb: int = Field(default=20, ge=1, validation_alias="MAX_AUDIO_SIZE_MB")
    ark_jwks_url: str | None = Field(default=None, validation_alias="ARK_JWKS_URL")
    ark_webhook_secret: str | None = Field(default=None, validation_alias="ARK_WEBHOOK_SECRET")
    ark_audience: str | None = Field(default=None, validation_alias="ARK_AUDIENCE")
    ark_issuer: str | None = Field(default=None, validation_alias="ARK_ISSUER")
    exclude_keywords: Annotated[tuple[str, ...], BeforeValidator(parse_comma_separated_tuple)] = Field(
        default=(), validation_alias="EXCLUDE_KEYWORDS"
    )
    trusted_proxies: Annotated[tuple[str, ...], BeforeValidator(parse_comma_separated_tuple)] = Field(
        default=(), validation_alias="TRUSTED_PROXIES"
    )

    # --- SQLite settings ---
    sqlite_db_path: Annotated[Path | None, BeforeValidator(validate_optional_path)] = Field(
        default=None, validation_alias="SQLITE_DB_PATH"
    )

    # --- Google Drive settings ---
    google_drive_credentials_path: Annotated[Path, BeforeValidator(validate_path)] = Field(
        default=Path("/app/config/service-key.json"), validation_alias="GOOGLE_DRIVE_CREDENTIALS_PATH"
    )
    google_drive_download_dir: Annotated[Path, BeforeValidator(validate_path)] = Field(
        default=Path("/app/downloads"), validation_alias="GOOGLE_DRIVE_DOWNLOAD_DIR"
    )
    google_drive_scopes: Annotated[tuple[str, ...], BeforeValidator(parse_comma_separated_tuple)] = Field(
        default=("https://www.googleapis.com/auth/drive.readonly",), validation_alias="GOOGLE_DRIVE_SCOPES"
    )

    # --- Deepgram settings ---
    deepgram_api_key: str = Field(default="", validation_alias="DEEPGRAM_API_KEY")
    deepgram_project_id: str = Field(default="", validation_alias="DEEPGRAM_PROJECT_ID")
    deepgram_model: str = Field(default="nova-3", validation_alias="DEEPGRAM_MODEL")
    deepgram_language: str = Field(default="ru", validation_alias="DEEPGRAM_LANGUAGE")
    deepgram_smart_format: bool = Field(default=True, validation_alias="DEEPGRAM_SMART_FORMAT")
    deepgram_punctuate: bool = Field(default=True, validation_alias="DEEPGRAM_PUNCTUATE")
    deepgram_utterances: bool = Field(default=True, validation_alias="DEEPGRAM_UTTERANCES")
    deepgram_paragraphs: bool = Field(default=True, validation_alias="DEEPGRAM_PARAGRAPHS")
    deepgram_diarize: bool = Field(default=True, validation_alias="DEEPGRAM_DIARIZE")
    deepgram_filler_words: bool = Field(default=False, validation_alias="DEEPGRAM_FILLER_WORDS")
    deepgram_base_url: str = Field(default="https://api.deepgram.com/v1/listen", validation_alias="DEEPGRAM_BASE_URL")

    # --- Embedding settings ---
    embedding_api_url: str = Field(default="", validation_alias="EMBEDDING_API_URL")
    embedding_api_token: str = Field(default="", validation_alias="EMBEDDING_API_TOKEN")
    embedding_model_id: str = Field(default="BAAI/bge-m3", validation_alias="EMBEDDING_MODEL_ID")
    embedding_dimension: int = Field(default=1024, ge=1, validation_alias="EMBEDDING_DIMENSION")
    embedding_cache_lru_size: int = Field(default=20, ge=0, validation_alias="EMBEDDING_CACHE_LRU_SIZE")
    embedding_provider: str = Field(default="custom", validation_alias="EMBEDDING_PROVIDER")
    embedding_openrouter_providers: Annotated[list[str] | None, BeforeValidator(parse_comma_separated_list)] = Field(
        default=None, validation_alias="EMBEDDING_OPENROUTER_PROVIDERS"
    )

    # --- Local AI settings ---
    local_embedding_model: str = Field(
        default="intfloat/multilingual-e5-small", validation_alias="LOCAL_EMBEDDING_MODEL"
    )
    local_embedding_dimension: int = Field(default=384, ge=1, validation_alias="LOCAL_EMBEDDING_DIMENSION")

    # --- Manticore settings ---
    manticore_url: str = Field(default="http://manticore:9308", validation_alias="MANTICORE_URL")
    manticore_table: str = Field(default="chunks", validation_alias="MANTICORE_TABLE")

    @field_validator("app_access_token")
    @classmethod
    def validate_access_token(cls, v: str) -> str:
        if v in (
            "change-me",
            "admin",
            "password",
            "change-me-to-a-secure-token",
            "change-me-to-something-secure",
            "Master",
        ):
            raise ValueError("APP_ACCESS_TOKEN must be changed from default and be secure")
        return v

    @field_validator("session_secret_key")
    @classmethod
    def validate_session_secret_key(cls, v: str) -> str:
        if v in (
            "change-me",
            "change-me-to-something-very-secret-and-long-enough",
            "generate-a-long-random-string-here",
        ):
            raise ValueError("SESSION_SECRET_KEY must be changed from default and be secure")
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Отключаем json-декодирование сложных типов для comma-separated списков,
        # чтобы BeforeValidator получал сырые строки.
        import types

        from pydantic.fields import FieldInfo

        for source in (env_settings, dotenv_settings):
            if hasattr(source, "decode_complex_value"):
                old_decode = source.decode_complex_value

                def new_decode(
                    self_source: Any,
                    field_name: str,
                    field: FieldInfo,
                    value: Any,
                    old_decode: Any = old_decode,
                ) -> Any:
                    if field_name in (
                        "exclude_keywords",
                        "google_drive_scopes",
                        "embedding_openrouter_providers",
                        "trusted_proxies",
                    ):
                        return value
                    return old_decode(field_name, field, value)

                source.decode_complex_value = types.MethodType(new_decode, source)  # type: ignore
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @property
    def resolved_db_path(self) -> Path:
        if self.sqlite_db_path:
            return resolve_host_path(self.sqlite_db_path)
        return resolve_host_path(self.app_data_dir / "pulsar.db")

    @property
    def raw_transcripts_dir(self) -> Path:
        return self.app_storage_dir / "transcripts" / "raw"

    def get_raw_transcript_path(self, source_file_id: str) -> Path:
        prefix = source_file_id[:2] if len(source_file_id) >= 2 else source_file_id
        return self.raw_transcripts_dir / prefix / f"{source_file_id}.json.gz"

    @property
    def normalized_transcripts_dir(self) -> Path:
        return self.app_storage_dir / "transcripts" / "normalized"

    def get_normalized_transcript_path(self, source_file_id: str) -> Path:
        prefix = source_file_id[:2] if len(source_file_id) >= 2 else source_file_id
        return self.normalized_transcripts_dir / prefix / f"{source_file_id}.json.gz"

    @property
    def downloads_dir(self) -> Path:
        return self.app_storage_dir / "downloads"

    @property
    def audio_dir(self) -> Path:
        return self.app_storage_dir / "audio"

    def resolve_path(self, path: str | Path | None) -> Path | None:
        if not path:
            return None
        p = Path(path)
        if p.is_absolute():
            return p
        return self.app_storage_dir / p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Единая точка загрузки конфигурации. Вызывается один раз, затем кэшируется."""
    return Settings()  # type: ignore # pyre-ignore
