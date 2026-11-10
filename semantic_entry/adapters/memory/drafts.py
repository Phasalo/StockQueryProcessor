from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
from threading import Lock, RLock
from typing import TYPE_CHECKING
from uuid import uuid4

from semantic_entry.core.access import PermissionDeniedError
from semantic_entry.core.drafts.models import (
    CommandDraft,
    DraftExpiredError,
    DraftNotFoundError,
    DraftState,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping
    from uuid import UUID

    from semantic_entry.core.access import Actor
    from semantic_entry.core.commands.models import PreparationResult


@dataclass
class _DraftEntry:
    draft: CommandDraft
    lock: RLock = field(default_factory=RLock)
    users: int = 0


class MemoryDraftStore:
    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._drafts: dict[UUID, _DraftEntry] = {}
        self._expirations: list[tuple[datetime, UUID]] = []
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
            self._purge_expired(now)
            self._drafts[draft.draft_id] = _DraftEntry(deepcopy(draft))
            heappush(self._expirations, (draft.expires_at, draft.draft_id))
        return deepcopy(draft)

    def purge_expired(self) -> int:
        now = self._now()
        with self._lock:
            return self._purge_expired(now)

    def get(self, draft_id: UUID, actor: Actor) -> CommandDraft:
        with self._entry(draft_id) as entry:
            return deepcopy(self._get_owned(entry, actor))

    @contextmanager
    def lock_current(self, draft_id: UUID, actor: Actor) -> Generator[CommandDraft, None, None]:
        with self._entry(draft_id) as entry:
            yield deepcopy(self._get_owned(entry, actor))

    @contextmanager
    def lock_confirmation(
        self, draft_id: UUID, expected_version: int, actor: Actor,
    ) -> Generator[CommandDraft, None, None]:
        with self._entry(draft_id) as entry:
            current = self._get_owned(entry, actor, allow_expired=entry.draft.state == DraftState.EXECUTED)
            current.require_version(expected_version)
            if current.state != DraftState.EXECUTED:
                current.require_ready(expected_version)
            yield deepcopy(current)

    def mark_executed(self, draft_id: UUID, expected_version: int, actor: Actor) -> CommandDraft:
        with self._entry(draft_id) as entry:
            current = self._get_owned(entry, actor, allow_expired=True)
            current.require_version(expected_version)
            if current.state != DraftState.EXECUTED:
                current.require_ready(expected_version)
                entry.draft = replace(current, state=DraftState.EXECUTED)
            return deepcopy(entry.draft)

    def update(
        self, draft_id: UUID, expected_version: int, actor: Actor,
        *, payload: Mapping[str, object], preparation: PreparationResult,
    ) -> CommandDraft:
        with self._entry(draft_id) as entry:
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
        with self._entry(draft_id) as entry:
            current = self._get_owned(entry, actor)
            current.require_editable(expected_version)
            cancelled = replace(current, version=current.version + 1, state=DraftState.CANCELLED)
            entry.draft = cancelled
            return deepcopy(cancelled)

    @contextmanager
    def _entry(self, draft_id: UUID) -> Generator[_DraftEntry, None, None]:
        with self._lock:
            try:
                entry = self._drafts[draft_id]
            except KeyError as error:
                raise DraftNotFoundError(draft_id) from error
            entry.users += 1
        try:
            with entry.lock:
                yield entry
        finally:
            with self._lock:
                entry.users -= 1

    def _purge_expired(self, now: datetime) -> int:
        deferred = []
        removed = 0
        while self._expirations and self._expirations[0][0] <= now:
            expiration = heappop(self._expirations)
            _, draft_id = expiration
            if self._drafts[draft_id].users:
                deferred.append(expiration)
            else:
                del self._drafts[draft_id]
                removed += 1
        for expiration in deferred:
            heappush(self._expirations, expiration)
        return removed

    def _get_owned(self, entry: _DraftEntry, actor: Actor, *, allow_expired: bool = False) -> CommandDraft:
        draft = entry.draft
        if draft.preparation.actor_id != actor.actor_id:
            raise PermissionDeniedError("Черновик принадлежит другому субъекту.")
        if not allow_expired and self._now() >= draft.expires_at:
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
