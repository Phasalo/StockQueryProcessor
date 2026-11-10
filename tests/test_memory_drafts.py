import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

from semantic_entry.adapters.memory.drafts import MemoryDraftStore
from semantic_entry.core.access import Actor, PermissionDeniedError
from semantic_entry.core.commands.models import (
    CommandModel,
    FieldError,
    PreparationResult,
)
from semantic_entry.core.drafts.models import DraftExpiredError, DraftState


class NoteInput(CommandModel):
    text: str
    tags: list[str] = []


class NotePreview(CommandModel):
    summary: str


class MemoryDraftStoreTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 12, tzinfo=UTC)
        self.clock = Mock(return_value=self.now)
        self.store = MemoryDraftStore(self.clock)
        self.actor = Actor("author")
        self.payload = {"text": "Note", "tags": ["first"]}
        self.preparation = PreparationResult(
            "notes.create", 1, self.actor.actor_id,
            NoteInput(**self.payload), preview=NotePreview(summary="Create Note"),
        )

    def create(self, preparation=None):
        return self.store.create(
            self.payload, preparation if preparation is not None else self.preparation,
            lifetime=timedelta(minutes=10),
        )

    def test_ready_draft_keeps_command_identity_version_and_lifetime(self):
        draft = self.create()
        self.assertEqual(draft.version, 1)
        self.assertEqual(draft.state, DraftState.READY)
        self.assertEqual(draft.created_at, self.now)
        self.assertEqual(draft.expires_at, self.now + timedelta(minutes=10))
        self.assertEqual(self.store.get(draft.draft_id, self.actor), draft)
        self.assertEqual(draft.preparation.command_name, "notes.create")
        self.assertEqual(draft.preparation.command_version, 1)
        self.assertEqual(draft.preparation.actor_id, "author")

    def test_partial_input_and_domain_errors_remain_editable_drafts(self):
        for prepared in (
            replace(self.preparation, validated_input=None, preview=None, errors=(FieldError(("text",), "missing", "Required"),)),
            replace(self.preparation, preview=None, errors=(FieldError(("text",), "not_found", "Unknown"),)),
        ):
            with self.subTest(prepared=prepared):
                draft = self.create(prepared)
                self.assertEqual(draft.state, DraftState.NEEDS_INPUT)
                self.assertEqual(draft.preparation.errors, prepared.errors)
                self.assertEqual(draft.payload, self.payload)

    def test_schema_validation_without_preview_is_not_ready(self):
        draft = self.create(replace(self.preparation, preview=None))
        self.assertTrue(draft.preparation.is_valid)
        self.assertEqual(draft.state, DraftState.NEEDS_INPUT)

    def test_draft_is_only_accessible_to_its_author(self):
        draft = self.create()
        with self.assertRaises(PermissionDeniedError):
            self.store.get(draft.draft_id, Actor("other"))
        self.assertEqual(self.store.get(draft.draft_id, self.actor), draft)

    def test_input_and_returned_snapshots_do_not_change_stored_draft(self):
        created = self.create()
        self.payload["tags"].append("source changed")
        self.preparation.validated_input.tags.append("input changed")
        self.preparation.preview.summary = "Preview changed"
        self.assertEqual(created.payload["tags"], ["first"])
        self.assertEqual(created.preparation.validated_input.tags, ["first"])
        self.assertEqual(created.preparation.preview.summary, "Create Note")
        created.payload["text"] = "Returned draft changed"
        first_read = self.store.get(created.draft_id, self.actor)
        self.assertEqual(first_read.payload, {"text": "Note", "tags": ["first"]})
        self.assertEqual(first_read.preparation.validated_input.tags, ["first"])
        self.assertEqual(first_read.preparation.preview.summary, "Create Note")
        first_read.payload["tags"].append("read changed")
        first_read.preparation.validated_input.text = "Changed"
        second_read = self.store.get(created.draft_id, self.actor)
        self.assertEqual(second_read.payload["tags"], ["first"])
        self.assertEqual(second_read.preparation.validated_input.text, "Note")

    def test_expiration_includes_exact_deadline(self):
        draft = self.create()
        self.clock.return_value = draft.expires_at - timedelta(microseconds=1)
        self.assertEqual(self.store.get(draft.draft_id, self.actor).draft_id, draft.draft_id)
        self.clock.return_value = draft.expires_at
        with self.assertRaises(DraftExpiredError):
            self.store.get(draft.draft_id, self.actor)

    def test_unknown_and_other_store_drafts_are_not_found(self):
        draft = self.create()
        with self.assertRaises(KeyError):
            self.store.get(uuid4(), self.actor)
        with self.assertRaises(KeyError):
            MemoryDraftStore(self.clock).get(draft.draft_id, self.actor)

    def test_invalid_lifetime_and_naive_clock_are_rejected(self):
        for lifetime in (timedelta(0), timedelta(seconds=-1)):
            with self.subTest(lifetime=lifetime):
                with self.assertRaises(ValueError):
                    self.store.create(self.payload, self.preparation, lifetime=lifetime)
        self.clock.return_value = datetime(2026, 11, 10, 12)
        with self.assertRaisesRegex(ValueError, "часовым поясом"):
            self.create()

    def test_timezone_is_normalized_and_metadata_is_frozen(self):
        self.clock.return_value = datetime(2026, 11, 10, 15, tzinfo=timezone(timedelta(hours=3)))
        draft = self.create()
        self.assertEqual(draft.created_at, self.now)
        self.assertIs(draft.created_at.tzinfo, UTC)
        with self.assertRaises(FrozenInstanceError):
            draft.version = 2


if __name__ == "__main__":
    unittest.main()
