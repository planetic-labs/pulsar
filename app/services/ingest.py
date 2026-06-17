from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.pipeline.base import PipelineStage, StageResult
from app.pipeline.download import DownloadStage
from app.pipeline.index import IndexStage
from app.pipeline.transcribe import TranscribeStage

logger = logging.getLogger("app.services.ingest")


class IngestService:
    """Оркестратор конвейера обработки видео (Pipeline Orchestrator)."""

    def __init__(self) -> None:
        self.stages: dict[str, PipelineStage] = {
            "stage_1_download": DownloadStage(),
            "ingest_video": DownloadStage(),  # Псевдоним для совместимости
            "stage_2_transcribe": TranscribeStage(),
            "stage_3_index": IndexStage(),
        }

    async def execute_stage(
        self,
        stage_type: str,
        task_id: int,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> StageResult:
        """Находит и запускает стадию конвейера для указанного типа задачи."""
        stage = self.stages.get(stage_type)
        if not stage:
            error_msg = f"Неизвестный тип стадии конвейера: {stage_type}"
            logger.error(error_msg)
            return StageResult(success=False, error=error_msg)

        logger.info(f"Запуск стадии {stage.stage_name()} для задачи {task_id}")
        return await stage.execute(task_id, payload, progress_callback)
