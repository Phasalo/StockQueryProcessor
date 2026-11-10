import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from unittest.mock import Mock
from uuid import uuid4

from semantic_entry.core import (
    Actor,
    CommandModel,
    FieldError,
    PermissionDeniedError,
    PreparationResult,
)
from semantic_entry.core.draft_manager import DraftManager
from semantic_entry.core.drafts import (
    CommandDraft,
    DraftClosedError,
    DraftExpiredError,
    DraftState,
    DraftVersionConflictError,
)
from semantic_entry.core.memory_drafts import MemoryDraftStore


class NoteInput(CommandModel):
    text: str


class NotePreview(CommandModel):
    summary: str


class DraftCancellationTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 12, tzinfo=UTC)
        self.clock = Mock(return_value=self.now)
        self.store = MemoryDraftStore(self.clock)
        self.actor = Actor("author")
        self.preparation = PreparationResult(
            "notes.create", 1, self.actor.actor_id,
            NoteInput(text="Note"), preview=NotePreview(summary="Create Note"),
        )
        self.preparer = Mock()
        self.manager = DraftManager(self.preparer, self.store)

    def create(self, preparation=None):
        return self.store.create(
            {"text": "Note"}, preparation if preparation is not None else self.preparation,
            lifetime=timedelta(minutes=10),
        )

    def test_ready_and_partial_drafts_can_be_cancelled_by_author(self):
        for prepared in (
            self.preparation,
            replace(self.preparation, validated_input=None, preview=None, errors=(FieldError(("text",), "missing", "Required"),)),
        ):
            with self.subTest(prepared=prepared):
                original = self.create(prepared)
                cancelled = self.manager.cancel(original.draft_id, 1, self.actor)
                self.assertEqual((cancelled.version, cancelled.state), (2, DraftState.CANCELLED))
                self.assertEqual(cancelled.draft_id, original.draft_id)
                self.assertEqual(cancelled.payload, original.payload)
                self.assertEqual(cancelled.preparation, original.preparation)
                self.assertEqual((cancelled.created_at, cancelled.expires_at), (original.created_at, original.expires_at))
                self.assertEqual(self.store.get(original.draft_id, self.actor), cancelled)
                cancelled.payload["text"] = "Changed copy"
                self.assertEqual(self.store.get(original.draft_id, self.actor).payload["text"], "Note")
        self.preparer.prepare.assert_not_called()

    def test_other_author_cannot_cancel(self):
        original = self.create()
        with self.assertRaises(PermissionDeniedError):
            self.manager.cancel(original.draft_id, 1, Actor("other"))
        self.assertEqual(self.store.get(original.draft_id, self.actor), original)

    def test_expired_draft_cannot_be_cancelled(self):
        original = self.create()
        self.clock.return_value = original.expires_at
        with self.assertRaises(DraftExpiredError):
            self.manager.cancel(original.draft_id, 1, self.actor)

    def test_stale_and_invalid_versions_leave_draft_unchanged(self):
        original = self.create()
        with self.assertRaises(DraftVersionConflictError):
            self.manager.cancel(original.draft_id, 2, self.actor)
        for version in (True, 0, "1"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    self.manager.cancel(original.draft_id, version, self.actor)
        self.assertEqual(self.store.get(original.draft_id, self.actor), original)

    def test_repeated_cancellation_does_not_create_another_version(self):
        original = self.create()
        cancelled = self.manager.cancel(original.draft_id, 1, self.actor)
        with self.assertRaises(DraftVersionConflictError):
            self.manager.cancel(original.draft_id, 1, self.actor)
        with self.assertRaises(DraftClosedError):
            self.manager.cancel(original.draft_id, 2, self.actor)
        self.assertEqual(self.store.get(original.draft_id, self.actor), cancelled)

    def test_cancelled_draft_cannot_be_revised(self):
        original = self.create()
        cancelled = self.manager.cancel(original.draft_id, 1, self.actor)
        with self.assertRaises(DraftClosedError):
            self.manager.revise(original.draft_id, 2, {"text": "Changed"}, self.actor)
        self.preparer.prepare.assert_not_called()
        self.assertEqual(self.store.get(original.draft_id, self.actor), cancelled)

    def test_concurrent_cancellations_change_state_once(self):
        original = self.create()
        barrier = Barrier(2)

        def cancel():
            barrier.wait(timeout=3)
            try:
                return self.manager.cancel(original.draft_id, 1, self.actor)
            except DraftVersionConflictError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(cancel) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sum(isinstance(result, CommandDraft) for result in results), 1)
        self.assertEqual(sum(isinstance(result, DraftVersionConflictError) for result in results), 1)
        stored = self.store.get(original.draft_id, self.actor)
        self.assertEqual((stored.version, stored.state), (2, DraftState.CANCELLED))

    def test_inflight_revision_cannot_overwrite_cancellation(self):
        original = self.create()
        started = Event()
        release = Event()

        def prepare(*args):
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Revision was not released")
            return self.preparation

        self.preparer.prepare.side_effect = prepare
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.manager.revise, original.draft_id, 1, {"text": "Changed"}, self.actor)
            try:
                self.assertTrue(started.wait(timeout=3))
                cancelled = self.manager.cancel(original.draft_id, 1, self.actor)
            finally:
                release.set()
            with self.assertRaises(DraftVersionConflictError):
                future.result(timeout=5)
        self.assertEqual(self.store.get(original.draft_id, self.actor), cancelled)

    def test_unknown_draft_is_not_found(self):
        with self.assertRaises(KeyError):
            self.manager.cancel(uuid4(), 1, self.actor)


if __name__ == "__main__":
    unittest.main()
