from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

from semantic_entry.core.contracts import PermissionDeniedError
from semantic_entry.core.drafts import CommandDraft, DraftExpiredError, DraftState

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from uuid import UUID

    from semantic_entry.core.contracts import Actor, PreparationResult


class MemoryDraftStore:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._drafts: dict[UUID, CommandDraft] = {}
        self._lock = Lock()

    def create(
        self, payload: Mapping[str, object], preparation: PreparationResult,
        *, lifetime: timedelta,
    ) -> CommandDraft:
        if lifetime <= timedelta(0):
            raise ValueError("Срок действия черновика должен быть положительным.")
        now = self._now()
        draft = CommandDraft(
            draft_id=uuid4(), version=1,
            state=self._state(preparation),
            payload=dict(payload), preparation=preparation,
            created_at=now, expires_at=now + lifetime,
        )
        with self._lock:
            self._drafts[draft.draft_id] = deepcopy(draft)
        return deepcopy(draft)

    def get(self, draft_id: UUID, actor: Actor) -> CommandDraft:
        with self._lock:
            return deepcopy(self._get_owned(draft_id, actor))

    def update(
        self, draft_id: UUID, expected_version: int, actor: Actor,
        *, payload: Mapping[str, object], preparation: PreparationResult,
    ) -> CommandDraft:
        with self._lock:
            current = self._get_owned(draft_id, actor)
            current.require_editable(expected_version)
            if (
                preparation.command_name != current.preparation.command_name
                or preparation.command_version != current.preparation.command_version
                or preparation.actor_id != current.preparation.actor_id
            ):
                raise ValueError("Команду, версию контракта и владельца черновика нельзя изменить.")
            updated = replace(
                current, version=current.version + 1, state=self._state(preparation),
                payload=dict(payload), preparation=preparation,
            )
            self._drafts[draft_id] = deepcopy(updated)
            return deepcopy(updated)

    def _get_owned(self, draft_id: UUID, actor: Actor) -> CommandDraft:
        draft = self._drafts[draft_id]
        if draft.preparation.actor_id != actor.actor_id:
            raise PermissionDeniedError("Черновик принадлежит другому субъекту.")
        if self._now() >= draft.expires_at:
            raise DraftExpiredError("Срок действия черновика истёк.")
        return draft

    @staticmethod
    def _state(preparation: PreparationResult) -> DraftState:
        return DraftState.READY if preparation.is_valid and preparation.preview is not None else DraftState.NEEDS_INPUT

    def _now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("Часы черновиков должны возвращать время с часовым поясом.")
        return now.astimezone(UTC)
