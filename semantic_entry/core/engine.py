from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from semantic_entry.core.contracts import (
    CommandEffect,
    CommandModel,
    CommandSpec,
    PermissionDeniedError,
)
from semantic_entry.core.draft_manager import DraftManager
from semantic_entry.core.drafts import (
    DraftClosedError,
    DraftExpiredError,
    DraftNotFoundError,
    DraftNotReadyError,
    DraftPreviewChangedError,
    DraftState,
    DraftVersionConflictError,
)
from semantic_entry.core.executions import ExecutionRecord, IdempotencyConflictError
from semantic_entry.core.preparation import validate_preview

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from datetime import timedelta
    from uuid import UUID

    from semantic_entry.core.catalog import CommandCatalog
    from semantic_entry.core.contracts import Actor
    from semantic_entry.core.drafts import CommandDraft
    from semantic_entry.core.memory_drafts import MemoryDraftStore
    from semantic_entry.core.ports.execution import CommandTransaction, WriteHandler
    from semantic_entry.core.preparation import CommandPreparer
    from semantic_entry.core.queries import QueryExecutor


InputT = TypeVar("InputT", bound=CommandModel)
PreviewT = TypeVar("PreviewT", bound=CommandModel)
ResultT = TypeVar("ResultT", bound=CommandModel)


@dataclass(frozen=True)
class _WriteBinding:
    spec: CommandSpec[Any, Any]
    preview_model: type[CommandModel]
    factory: Callable[[CommandTransaction], WriteHandler[Any, Any, Any]]


class Engine:
    def __init__(
        self, catalog: CommandCatalog, preparer: CommandPreparer, queries: QueryExecutor,
        drafts: MemoryDraftStore, transaction_factory: Callable[[], AbstractContextManager[CommandTransaction]],
    ):
        self._catalog = catalog
        self._preparer = preparer
        self._queries = queries
        self._drafts = drafts
        self._manager = DraftManager(preparer, drafts)
        self._transaction_factory = transaction_factory
        self._writes: dict[tuple[str, int], _WriteBinding] = {}

    def register_write(
        self, spec: CommandSpec[InputT, ResultT], preview_model: type[PreviewT],
        factory: Callable[[CommandTransaction], WriteHandler[InputT, PreviewT, ResultT]],
    ) -> None:
        if not isinstance(spec, CommandSpec) or spec.effect != CommandEffect.WRITE:
            raise ValueError("Обработчик проведения требует спецификацию WRITE.")
        if not isinstance(preview_model, type) or not issubclass(preview_model, CommandModel):
            raise TypeError("Схема preview должна быть классом CommandModel.")
        if preview_model.model_config.get("extra") != "forbid":
            raise ValueError("Схема preview должна запрещать лишние поля.")
        if not callable(factory):
            raise TypeError("Фабрика обработчика должна быть вызываемым объектом.")
        key = spec.name, spec.version
        if key in self._writes:
            raise ValueError(f"Обработчик {spec.key} уже зарегистрирован.")
        self._writes[key] = _WriteBinding(spec, preview_model, factory)

    def prepare(
        self, name: str, version: int, payload: Mapping[str, object], actor: Actor,
        *, lifetime: timedelta,
    ) -> CommandDraft:
        spec = self._catalog.get(name, version, actor)
        binding = self._binding(spec)
        if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
            raise TypeError("Параметры черновика должны быть объектом со строковыми ключами.")
        prepared = self._preparer.prepare(name, version, payload, actor)
        if prepared.is_valid and not isinstance(prepared.preview, binding.preview_model):
            raise ValueError("Для проведения команды требуется зарегистрированный preview.")
        return self._drafts.create(payload, prepared, lifetime=lifetime)

    def get_draft(self, draft_id: UUID, actor: Actor) -> CommandDraft:
        return self._drafts.get(draft_id, actor)

    def revise(self, draft_id: UUID, expected_version: int, patch: Mapping[str, object], actor: Actor) -> CommandDraft:
        with self._drafts.lock_current(draft_id, actor) as current:
            current.require_editable(expected_version)
            self._check_unexecuted(current, actor)
            return self._manager.revise(draft_id, expected_version, patch, actor)

    def cancel(self, draft_id: UUID, expected_version: int, actor: Actor) -> CommandDraft:
        with self._drafts.lock_current(draft_id, actor) as current:
            current.require_editable(expected_version)
            self._check_unexecuted(current, actor)
            return self._manager.cancel(draft_id, expected_version, actor)

    def _check_unexecuted(self, draft: CommandDraft, actor: Actor) -> None:
        with self._transaction_factory() as transaction:
            existing = transaction.executions.get_by_draft(draft.draft_id)
        if existing is not None:
            self._check_record(existing, draft.draft_id, draft.version, actor)
            self._drafts.mark_executed(draft.draft_id, draft.version, actor)
            raise DraftClosedError("Операция уже проведена; черновик нельзя изменить или отменить.")

    def query(self, name: str, version: int, payload: Mapping[str, Any], actor: Actor) -> CommandModel:
        return self._queries.query(name, version, payload, actor)

    def confirm(self, draft_id: UUID, expected_version: int, idempotency_key: str, actor: Actor) -> CommandModel:
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Ожидаемая версия должна быть положительным целым числом.")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("Ключ идемпотентности должен быть непустой строкой.")
        with ExitStack() as stack:
            try:
                draft = stack.enter_context(self._drafts.lock_confirmation(draft_id, expected_version, actor))
            except DraftNotFoundError:
                return self._recover(draft_id, expected_version, idempotency_key, actor)
            except (DraftExpiredError, DraftVersionConflictError, DraftClosedError) as unavailable:
                try:
                    return self._recover(draft_id, expected_version, idempotency_key, actor)
                except DraftNotFoundError:
                    raise unavailable from None
            return self._confirm_locked(draft, idempotency_key, actor)

    def _confirm_locked(self, draft: CommandDraft, key: str, actor: Actor) -> CommandModel:
        spec = self._catalog.get(draft.preparation.command_name, draft.preparation.command_version, actor)
        binding = self._binding(spec)
        request = self._request(draft)
        signature = hashlib.sha256(json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode()).hexdigest()
        with self._transaction_factory() as transaction:
            transaction.executions.lock(actor.actor_id, key, draft.draft_id)
            existing = self._existing(transaction, draft, key, actor, signature)
            if existing is not None:
                result = spec.validate_result(existing.result)
            else:
                if draft.state == DraftState.EXECUTED:
                    raise RuntimeError("Сохранённый результат исполненного черновика не найден.")
                result = self._execute(transaction, binding, draft, actor)
                record = ExecutionRecord(
                    uuid4(), draft.draft_id, draft.version, actor.actor_id,
                    spec.name, spec.version, signature, request, result.model_dump(mode="json", round_trip=True),
                )
                transaction.executions.add(record, key)
        self._drafts.mark_executed(draft.draft_id, draft.version, actor)
        return result

    def _existing(
        self, transaction: CommandTransaction, draft: CommandDraft, key: str, actor: Actor, signature: str,
    ) -> ExecutionRecord | None:
        keyed = transaction.executions.get_by_key(actor.actor_id, key)
        existing = keyed if keyed is not None else transaction.executions.get_by_draft(draft.draft_id)
        if existing is None:
            return None
        self._check_record(existing, draft.draft_id, draft.version, actor, signature)
        if keyed is None:
            transaction.executions.bind_key(existing, key)
        return existing

    def _execute(
        self, transaction: CommandTransaction, binding: _WriteBinding, draft: CommandDraft, actor: Actor,
    ) -> CommandModel:
        validated = draft.preparation.validated_input
        if validated is None:
            raise DraftNotReadyError("Черновик не содержит проверенных параметров.")
        parameters = binding.spec.validate_input(validated.model_dump(round_trip=True))
        handler = binding.factory(transaction)
        fresh = validate_preview(handler.preview(parameters, actor), binding.preview_model)
        if not fresh.is_valid or fresh.preview != draft.preparation.preview:
            prepared = replace(draft.preparation, validated_input=parameters, errors=fresh.errors, preview=fresh.preview)
            updated = self._drafts.update(
                draft.draft_id, draft.version, actor, payload=draft.payload, preparation=prepared,
            )
            raise DraftPreviewChangedError(updated)
        result = handler.execute(parameters, actor)
        if not isinstance(result, binding.spec.result_model):
            raise TypeError("Обработчик должен вернуть модель результата своей команды.")
        checked = binding.spec.validate_result(result.model_dump(round_trip=True))
        return binding.spec.validate_result(checked.model_dump(mode="json", round_trip=True))

    def _recover(self, draft_id: UUID, version: int, key: str, actor: Actor) -> CommandModel:
        with self._transaction_factory() as transaction:
            transaction.executions.lock(actor.actor_id, key, draft_id)
            keyed = transaction.executions.get_by_key(actor.actor_id, key)
            existing = keyed if keyed is not None else transaction.executions.get_by_draft(draft_id)
            if existing is None:
                raise DraftNotFoundError(draft_id)
            self._check_record(existing, draft_id, version, actor)
            spec = self._catalog.get(existing.command_name, existing.command_version, actor)
            if spec.effect != CommandEffect.WRITE:
                raise ValueError("Сохранённое исполнение требует спецификацию WRITE.")
            result = spec.validate_result(existing.result)
            if keyed is None:
                transaction.executions.bind_key(existing, key)
        return result

    def _binding(self, spec: CommandSpec[Any, Any]) -> _WriteBinding:
        if spec.effect != CommandEffect.WRITE:
            raise ValueError("Подготовка и подтверждение доступны только для WRITE.")
        binding = self._writes[spec.name, spec.version]
        if binding.spec is not spec:
            raise ValueError("Обработчик зарегистрирован для другой спецификации команды.")
        return binding

    @staticmethod
    def _check_record(
        execution: ExecutionRecord, draft_id: UUID, version: int, actor: Actor, signature: str | None = None,
    ) -> None:
        if execution.actor_id != actor.actor_id:
            raise PermissionDeniedError("Исполнение принадлежит другому субъекту.")
        if execution.draft_id != draft_id or execution.draft_version != version:
            raise IdempotencyConflictError("Ключ или черновик уже связан с другим запросом.")
        if signature is not None and execution.request_hash != signature:
            raise IdempotencyConflictError("Содержимое запроса отличается от сохранённого исполнения.")

    @staticmethod
    def _request(draft: CommandDraft) -> dict[str, Any]:
        prepared = draft.preparation
        if prepared.validated_input is None or prepared.preview is None:
            raise DraftNotReadyError("Черновик не содержит проверенных параметров и preview.")
        return {
            "draft_id": str(draft.draft_id), "draft_version": draft.version,
            "actor_id": draft.preparation.actor_id,
            "command_name": draft.preparation.command_name,
            "command_version": draft.preparation.command_version,
            "parameters": prepared.validated_input.model_dump(mode="json", round_trip=True),
            "preview": prepared.preview.model_dump(mode="json", round_trip=True),
        }
