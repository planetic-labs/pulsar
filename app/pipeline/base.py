from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class StageResult:
    """Результат выполнения стадии конвейера."""

    success: bool
    next_payload: dict[str, Any] | None = None
    error: str | None = None
    status: str | None = None  # Дополнительный статус выполнения (например, для пропущенных задач)


class PipelineStage(ABC):
    """Базовый класс для всех стадий конвейера."""

    @abstractmethod
    async def execute(
        self,
        task_id: int,
        payload: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> StageResult:
        """Выполнить стадию конвейера.

        Args:
            task_id: Идентификатор задачи.
            payload: Данные задачи.
            progress_callback: Коллбек для отправки обновлений прогресса в UI.
        """
        pass

    @abstractmethod
    def stage_name(self) -> str:
        """Имя стадии для логирования и отслеживания прогресса."""
        pass
