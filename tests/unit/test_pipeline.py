from __future__ import annotations

from app.pipeline.download import DownloadStage
from app.pipeline.index import IndexStage
from app.pipeline.transcribe import TranscribeStage
from app.services.ingest import IngestService
from app.services.task_queue import TaskQueueService


def test_pipeline_stages_names() -> None:
    """Проверяет корректность названий стадий конвейера."""
    download_stage = DownloadStage()
    transcribe_stage = TranscribeStage()
    index_stage = IndexStage()

    assert download_stage.stage_name() == "stage_1_download"
    assert transcribe_stage.stage_name() == "stage_2_transcribe"
    assert index_stage.stage_name() == "stage_3_index"


def test_ingest_service_initialization() -> None:
    """Проверяет правильность инициализации IngestService и регистрации стадий."""
    service = IngestService()
    assert "stage_1_download" in service.stages
    assert "stage_2_transcribe" in service.stages
    assert "stage_3_index" in service.stages
    assert isinstance(service.stages["stage_1_download"], DownloadStage)
    assert isinstance(service.stages["stage_2_transcribe"], TranscribeStage)
    assert isinstance(service.stages["stage_3_index"], IndexStage)


def test_task_queue_service_initialization() -> None:
    """Проверяет инициализацию сервиса очереди задач."""
    queue_service = TaskQueueService()
    assert queue_service.db_settings is not None
