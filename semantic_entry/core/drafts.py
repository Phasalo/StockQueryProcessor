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


@dataclass(frozen=True)
class CommandDraft:
    draft_id: UUID
    version: int
    state: DraftState
    payload: dict[str, Any]
    preparation: PreparationResult
    created_at: datetime
    expires_at: datetime
