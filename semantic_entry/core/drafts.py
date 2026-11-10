from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from semantic_entry.core.contracts import PreparationResult


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
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Ожидаемая версия должна быть положительным целым числом.")
        if self.version != expected_version:
            raise DraftVersionConflictError(
                f"Ожидалась версия {expected_version}, текущая версия {self.version}.",
            )
        if self.state not in (DraftState.NEEDS_INPUT, DraftState.READY):
            raise DraftClosedError("Закрытый черновик нельзя изменить.")
