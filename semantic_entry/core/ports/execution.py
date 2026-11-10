from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypeVar

from semantic_entry.core.contracts import CommandModel

if TYPE_CHECKING:
    from uuid import UUID

    from semantic_entry.core.contracts import Actor, PreviewResult
    from semantic_entry.core.executions import ExecutionRecord


InputT = TypeVar("InputT", bound=CommandModel, contravariant=True)
PreviewT = TypeVar("PreviewT", bound=CommandModel)
ResultT = TypeVar("ResultT", bound=CommandModel, covariant=True)


class ExecutionStore(Protocol):
    def lock(self, actor_id: str, key: str, draft_id: UUID) -> None: ...
    def get_by_key(self, actor_id: str, key: str) -> ExecutionRecord | None: ...
    def get_by_draft(self, draft_id: UUID) -> ExecutionRecord | None: ...
    def add(self, execution: ExecutionRecord, key: str) -> None: ...
    def bind_key(self, execution: ExecutionRecord, key: str) -> None: ...


class CommandTransaction(Protocol):
    @property
    def executions(self) -> ExecutionStore: ...


class WriteHandler(Protocol[InputT, PreviewT, ResultT]):
    def preview(self, parameters: InputT, actor: Actor) -> PreviewResult[PreviewT]: ...
    def execute(self, parameters: InputT, actor: Actor) -> ResultT: ...
