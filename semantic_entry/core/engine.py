from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from semantic_entry.core.commands.models import CommandModel
from semantic_entry.core.drafts.manager import DraftManager
from semantic_entry.core.executions.confirmation import CommandConfirmer

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from datetime import timedelta
    from uuid import UUID

    from semantic_entry.core.access import Actor
    from semantic_entry.core.commands.catalog import CommandCatalog
    from semantic_entry.core.commands.models import CommandSpec
    from semantic_entry.core.commands.preparation import CommandPreparer
    from semantic_entry.core.commands.queries import QueryExecutor
    from semantic_entry.core.drafts.models import CommandDraft
    from semantic_entry.core.ports.drafts import DraftStore
    from semantic_entry.core.ports.execution import CommandTransaction, WriteHandler

InputT = TypeVar("InputT", bound=CommandModel)
PreviewT = TypeVar("PreviewT", bound=CommandModel)
ResultT = TypeVar("ResultT", bound=CommandModel)


class Engine:
    def __init__(
        self, catalog: CommandCatalog, preparer: CommandPreparer, queries: QueryExecutor,
        drafts: DraftStore, transaction_factory: Callable[[], AbstractContextManager[CommandTransaction]],
    ):
        self._catalog = catalog
        self._preparer = preparer
        self._queries = queries
        self._drafts = drafts
        self._manager = DraftManager(preparer, drafts)
        self._confirmer = CommandConfirmer(catalog, drafts, transaction_factory)

    def register_write(
        self, spec: CommandSpec[InputT, ResultT], preview_model: type[PreviewT],
        factory: Callable[[CommandTransaction], WriteHandler[InputT, PreviewT, ResultT]],
    ) -> None:
        self._confirmer.register(spec, preview_model, factory)

    def prepare(
        self, name: str, version: int, payload: Mapping[str, object], actor: Actor,
        *, lifetime: timedelta,
    ) -> CommandDraft:
        spec = self._catalog.get(name, version, actor)
        preview_model = self._confirmer.require_registered(spec)
        if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
            raise TypeError("Параметры черновика должны быть объектом со строковыми ключами.")
        prepared = self._preparer.prepare(name, version, payload, actor)
        if prepared.is_valid and not isinstance(prepared.preview, preview_model):
            raise ValueError("Для проведения команды требуется зарегистрированный preview.")
        return self._drafts.create(payload, prepared, lifetime=lifetime)

    def get_draft(self, draft_id: UUID, actor: Actor) -> CommandDraft:
        return self._drafts.get(draft_id, actor)

    def revise(self, draft_id: UUID, expected_version: int, patch: Mapping[str, object], actor: Actor) -> CommandDraft:
        with self._drafts.lock_current(draft_id, actor) as current:
            current.require_editable(expected_version)
            self._confirmer.ensure_unexecuted(current, actor)
            return self._manager.revise(draft_id, expected_version, patch, actor)

    def cancel(self, draft_id: UUID, expected_version: int, actor: Actor) -> CommandDraft:
        with self._drafts.lock_current(draft_id, actor) as current:
            current.require_editable(expected_version)
            self._confirmer.ensure_unexecuted(current, actor)
            return self._manager.cancel(draft_id, expected_version, actor)

    def query(self, name: str, version: int, payload: Mapping[str, Any], actor: Actor) -> CommandModel:
        return self._queries.query(name, version, payload, actor)

    def confirm(self, draft_id: UUID, expected_version: int, idempotency_key: str, actor: Actor) -> CommandModel:
        return self._confirmer.confirm(draft_id, expected_version, idempotency_key, actor)
