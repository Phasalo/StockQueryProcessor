from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from semantic_entry.core.commands.models import PreparationResult


class DraftState(StrEnum):
    NEEDS_INPUT = "NEEDS_INPUT"
    READY = "READY"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class DraftExpiredError(ValueError):
    pass


class DraftVersionConflictError(ValueError):
    pass


class DraftClosedError(ValueError):
    pass


class DraftNotReadyError(ValueError):
    pass


class DraftNotFoundError(KeyError):
    pass


@dataclass(frozen=True)
class CommandDraft:
    draft_id: UUID
    version: int
    state: DraftState
    payload: dict[str, Any]
    preparation: PreparationResult
    created_at: datetime
    expires_at: datetime

    def require_editable(self, expected_version: int) -> None:
        self.require_version(expected_version)
        if self.state not in (DraftState.NEEDS_INPUT, DraftState.READY):
            raise DraftClosedError("Закрытый черновик нельзя изменить.")

    def require_version(self, expected_version: int) -> None:
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Ожидаемая версия должна быть положительным целым числом.")
        if self.version != expected_version:
            raise DraftVersionConflictError(
                f"Ожидалась версия {expected_version}, текущая версия {self.version}.",
            )

    def require_ready(self, expected_version: int) -> None:
        self.require_editable(expected_version)
        if (
            self.state != DraftState.READY
            or not self.preparation.is_valid
            or self.preparation.preview is None
        ):
            raise DraftNotReadyError("Черновик ещё не готов к подтверждению.")


class DraftPreviewChangedError(ValueError):
    def __init__(self, draft: CommandDraft):
        super().__init__("Условия операции изменились. Подтвердите новую версию карточки.")
        self.draft = draft
