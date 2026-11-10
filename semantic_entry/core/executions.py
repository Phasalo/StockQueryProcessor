from dataclasses import dataclass
from typing import Any
from uuid import UUID


class IdempotencyConflictError(ValueError):
    pass


@dataclass(frozen=True)
class ExecutionRecord:
    execution_id: UUID
    draft_id: UUID
    draft_version: int
    actor_id: str
    command_name: str
    command_version: int
    request_hash: str
    request: dict[str, Any]
    result: dict[str, Any]
