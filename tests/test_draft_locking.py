import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest.mock import Mock

from semantic_entry.adapters.memory.drafts import MemoryDraftStore
from semantic_entry.core.access import Actor, PermissionDeniedError
from semantic_entry.core.commands.models import (
    CommandModel,
    FieldError,
    PreparationResult,
)
from semantic_entry.core.drafts.models import (
    DraftClosedError,
    DraftExpiredError,
    DraftNotFoundError,
    DraftNotReadyError,
    DraftState,
    DraftVersionConflictError,
)


class NoteInput(CommandModel):
    text: str


class NotePreview(CommandModel):
    summary: str


class DraftLockingTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 12, tzinfo=UTC)
        self.clock = Mock(return_value=self.now)
        self.store = MemoryDraftStore(self.clock)
        self.actor = Actor("author")
        self.prepared = PreparationResult(
            "notes.create", 1, self.actor.actor_id,
            NoteInput(text="Note"), preview=NotePreview(summary="Create Note"),
        )

    def create(self, preparation=None):
        return self.store.create(
            {"text": "Note"}, preparation if preparation is not None else self.prepared,
            lifetime=timedelta(minutes=10),
        )

    def test_lock_yields_isolated_ready_snapshot(self):
        original = self.create()
        with self.store.lock_confirmation(original.draft_id, 1, self.actor) as locked:
            self.assertEqual(locked, original)
            locked.payload["text"] = "Changed"
            locked.preparation.preview.summary = "Changed"
            self.assertEqual(self.store.get(original.draft_id, self.actor), original)
        self.assertEqual(self.store.get(original.draft_id, self.actor), original)

    def test_invalid_versions_and_other_author_cannot_enter_context(self):
        original = self.create()
        with self.assertRaises(PermissionDeniedError):
            with self.store.lock_confirmation(original.draft_id, 1, Actor("other")):
                self.fail("Context must not be entered")
        with self.assertRaises(DraftVersionConflictError):
            with self.store.lock_confirmation(original.draft_id, 2, self.actor):
                self.fail("Context must not be entered")
        for version in (True, 0, "1"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    with self.store.lock_confirmation(original.draft_id, version, self.actor):
                        self.fail("Context must not be entered")

    def test_partial_and_schema_only_drafts_are_not_ready(self):
        for prepared in (
            replace(self.prepared, preview=None),
            replace(self.prepared, preview=None, errors=(FieldError(("text",), "not_found", "Unknown"),)),
        ):
            with self.subTest(prepared=prepared):
                original = self.create(prepared)
                with self.assertRaises(DraftNotReadyError):
                    with self.store.lock_confirmation(original.draft_id, 1, self.actor):
                        self.fail("Context must not be entered")

    def test_cancelled_and_expired_drafts_cannot_enter_context(self):
        original = self.create()
        self.store.cancel(original.draft_id, 1, self.actor)
        with self.assertRaises(DraftClosedError):
            with self.store.lock_confirmation(original.draft_id, 2, self.actor):
                self.fail("Context must not be entered")
        expired = self.create()
        self.clock.return_value = expired.expires_at
        with self.assertRaises(DraftExpiredError):
            with self.store.lock_confirmation(expired.draft_id, 1, self.actor):
                self.fail("Context must not be entered")

    def test_cancellation_waits_until_context_finishes(self):
        original = self.create()
        started = Event()
        finished = Event()

        def cancel():
            started.set()
            try:
                return self.store.cancel(original.draft_id, 1, self.actor)
            finally:
                finished.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.store.lock_confirmation(original.draft_id, 1, self.actor):
                future = executor.submit(cancel)
                self.assertTrue(started.wait(timeout=3))
                self.assertFalse(finished.wait(timeout=0.05))
            cancelled = future.result(timeout=3)
        self.assertEqual(cancelled.state, DraftState.CANCELLED)

    def test_update_waits_until_context_finishes(self):
        original = self.create()
        started = Event()
        finished = Event()

        def update():
            started.set()
            try:
                return self.store.update(
                    original.draft_id, 1, self.actor,
                    payload={"text": "Changed"}, preparation=self.prepared,
                )
            finally:
                finished.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.store.lock_confirmation(original.draft_id, 1, self.actor):
                future = executor.submit(update)
                self.assertTrue(started.wait(timeout=3))
                self.assertFalse(finished.wait(timeout=0.05))
            revised = future.result(timeout=3)
        self.assertEqual(revised.version, 2)

    def test_other_drafts_remain_available_while_context_is_held(self):
        original = self.create()
        other = self.create()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.store.lock_confirmation(original.draft_id, 1, self.actor):
                future = executor.submit(self.store.cancel, other.draft_id, 1, self.actor)
                cancelled = future.result(timeout=3)
        self.assertEqual(cancelled.state, DraftState.CANCELLED)
        self.assertEqual(self.store.get(original.draft_id, self.actor), original)

    def test_lock_is_released_after_exception(self):
        original = self.create()
        with self.assertRaisesRegex(RuntimeError, "Transaction failed"):
            with self.store.lock_confirmation(original.draft_id, 1, self.actor):
                raise RuntimeError("Transaction failed")
        with ThreadPoolExecutor(max_workers=1) as executor:
            cancelled = executor.submit(self.store.cancel, original.draft_id, 1, self.actor).result(timeout=3)
        self.assertEqual(cancelled.state, DraftState.CANCELLED)

    def test_cleanup_skips_active_confirmation_without_blocking_other_drafts(self):
        active = self.create()
        expired = self.create()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.store.lock_confirmation(active.draft_id, 1, self.actor):
                self.clock.return_value = active.expires_at
                self.assertEqual(executor.submit(self.store.purge_expired).result(timeout=3), 1)
                executed = self.store.mark_executed(active.draft_id, 1, self.actor)
                self.assertEqual(executed.state, DraftState.EXECUTED)
                with self.assertRaises(DraftNotFoundError):
                    self.store.get(expired.draft_id, self.actor)
        self.assertEqual(self.store.purge_expired(), 1)
        self.assertEqual(self.store.purge_expired(), 0)

    def test_failed_context_does_not_prevent_later_cleanup(self):
        draft = self.create()
        with self.assertRaisesRegex(RuntimeError, "Stop confirmation"):
            with self.store.lock_confirmation(draft.draft_id, 1, self.actor):
                self.clock.return_value = draft.expires_at
                self.assertEqual(self.store.purge_expired(), 0)
                raise RuntimeError("Stop confirmation")
        self.assertEqual(self.store.purge_expired(), 1)


if __name__ == "__main__":
    unittest.main()
