from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from threading import Lock, RLock
from typing import TYPE_CHECKING
from uuid import uuid4

from semantic_entry.core.contracts import PermissionDeniedError
from semantic_entry.core.drafts import CommandDraft, DraftExpiredError, DraftState

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping
    from uuid import UUID

    from semantic_entry.core.contracts import Actor, PreparationResult


@dataclass
class _DraftEntry:
    draft: CommandDraft
    lock: RLock = field(default_factory=RLock)


class MemoryDraftStore:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._drafts: dict[UUID, _DraftEntry] = {}
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
            self._drafts[draft.draft_id] = _DraftEntry(deepcopy(draft))
        return deepcopy(draft)

    def get(self, draft_id: UUID, actor: Actor) -> CommandDraft:
        entry = self._entry(draft_id)
        with entry.lock:
            return deepcopy(self._get_owned(entry, actor))

    @contextmanager
    def lock_ready(
        self, draft_id: UUID, expected_version: int, actor: Actor,
    ) -> Generator[CommandDraft, None, None]:
        entry = self._entry(draft_id)
        with entry.lock:
            current = self._get_owned(entry, actor)
            current.require_ready(expected_version)
            yield deepcopy(current)

    def update(
        self, draft_id: UUID, expected_version: int, actor: Actor,
        *, payload: Mapping[str, object], preparation: PreparationResult,
    ) -> CommandDraft:
        entry = self._entry(draft_id)
        with entry.lock:
            current = self._get_owned(entry, actor)
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
            entry.draft = deepcopy(updated)
            return deepcopy(updated)

    def cancel(self, draft_id: UUID, expected_version: int, actor: Actor) -> CommandDraft:
        entry = self._entry(draft_id)
        with entry.lock:
            current = self._get_owned(entry, actor)
            current.require_editable(expected_version)
            cancelled = replace(current, version=current.version + 1, state=DraftState.CANCELLED)
            entry.draft = cancelled
            return deepcopy(cancelled)

    def _entry(self, draft_id: UUID) -> _DraftEntry:
        with self._lock:
            return self._drafts[draft_id]

    def _get_owned(self, entry: _DraftEntry, actor: Actor) -> CommandDraft:
        draft = entry.draft
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
