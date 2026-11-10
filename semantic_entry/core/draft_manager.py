from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from semantic_entry.core.contracts import Actor
    from semantic_entry.core.drafts import CommandDraft
    from semantic_entry.core.memory_drafts import MemoryDraftStore
    from semantic_entry.core.preparation import CommandPreparer


class DraftManager:
    def __init__(self, preparer: CommandPreparer, store: MemoryDraftStore):
        self._preparer = preparer
        self._store = store

    def revise(
        self, draft_id: UUID, expected_version: int,
        patch: Mapping[str, object], actor: Actor,
    ) -> CommandDraft:
        current = self._store.get(draft_id, actor)
        current.require_editable(expected_version)
        if not isinstance(patch, Mapping) or any(not isinstance(key, str) for key in patch):
            raise TypeError("Изменения черновика должны быть объектом со строковыми ключами.")
        payload = {**current.payload, **deepcopy(dict(patch))}
        prepared = self._preparer.prepare(
            current.preparation.command_name, current.preparation.command_version, payload, actor,
        )
        return self._store.update(
            draft_id, expected_version, actor, payload=payload, preparation=prepared,
        )

    def cancel(self, draft_id: UUID, expected_version: int, actor: Actor) -> CommandDraft:
        return self._store.cancel(draft_id, expected_version, actor)
