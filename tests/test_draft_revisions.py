import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import Mock

from semantic_entry.adapters.memory.drafts import MemoryDraftStore
from semantic_entry.core.access import Actor, PermissionDeniedError
from semantic_entry.core.commands.catalog import CommandCatalog
from semantic_entry.core.commands.models import (
    CommandEffect,
    CommandModel,
    CommandSpec,
    FieldError,
    PreviewResult,
)
from semantic_entry.core.commands.preparation import CommandPreparer
from semantic_entry.core.commands.registry import CommandRegistry
from semantic_entry.core.drafts.manager import DraftManager
from semantic_entry.core.drafts.models import (
    CommandDraft,
    DraftClosedError,
    DraftExpiredError,
    DraftState,
    DraftVersionConflictError,
)


class NoteInput(CommandModel):
    text: str
    tags: list[str] = []


class NotePreview(CommandModel):
    summary: str


class DraftRevisionTest(unittest.TestCase):
    def setUp(self):
        self.spec = CommandSpec(
            "notes.create", 1, CommandEffect.WRITE, NoteInput, NotePreview,
            "Создать заметку", "notes.write",
        )
        registry = CommandRegistry()
        registry.register(self.spec)
        self.authorizer = Mock()
        self.authorizer.is_allowed.return_value = True
        self.preparer = CommandPreparer(CommandCatalog(registry, self.authorizer))
        self.preview = Mock(side_effect=self.build_preview)
        self.preparer.register_preview(self.spec, NotePreview, self.preview)
        self.now = datetime(2026, 11, 10, 12, tzinfo=UTC)
        self.clock = Mock(return_value=self.now)
        self.store = MemoryDraftStore(self.clock)
        self.manager = DraftManager(self.preparer, self.store)
        self.actor = Actor("author")

    @staticmethod
    def build_preview(parameters, actor):
        return PreviewResult[NotePreview](preview=NotePreview(summary=f"Create {parameters.text}"))

    def create(self, payload=None):
        payload = payload if payload is not None else {"text": "First", "tags": ["one"]}
        prepared = self.preparer.prepare(self.spec.name, 1, payload, self.actor)
        return self.store.create(payload, prepared, lifetime=timedelta(minutes=10))

    def test_revision_rebuilds_preview_and_preserves_identity_and_deadline(self):
        original = self.create()
        revised = self.manager.revise(original.draft_id, 1, {"text": "Second"}, self.actor)
        self.assertEqual(revised.draft_id, original.draft_id)
        self.assertEqual(revised.version, 2)
        self.assertEqual(revised.state, DraftState.READY)
        self.assertEqual(revised.preparation.preview.summary, "Create Second")
        self.assertEqual(revised.payload, {"text": "Second", "tags": ["one"]})
        self.assertEqual((revised.created_at, revised.expires_at), (original.created_at, original.expires_at))
        self.assertEqual(revised.preparation.command_version, 1)
        self.assertEqual(original.preparation.preview.summary, "Create First")
        self.assertEqual(self.store.get(original.draft_id, self.actor), revised)

    def test_invalid_revision_invalidates_previous_preview_and_can_be_corrected(self):
        original = self.create()
        invalid = self.manager.revise(original.draft_id, 1, {"text": None}, self.actor)
        self.assertEqual(invalid.version, 2)
        self.assertEqual(invalid.state, DraftState.NEEDS_INPUT)
        self.assertIsNone(invalid.preparation.preview)
        self.assertEqual([error.path for error in invalid.preparation.errors], [("text",)])
        corrected = self.manager.revise(original.draft_id, 2, {"text": "Fixed"}, self.actor)
        self.assertEqual((corrected.version, corrected.state), (3, DraftState.READY))
        self.assertEqual(corrected.preparation.preview.summary, "Create Fixed")

    def test_partial_draft_can_be_completed(self):
        original = self.create({})
        self.assertEqual(original.state, DraftState.NEEDS_INPUT)
        revised = self.manager.revise(original.draft_id, 1, {"text": "Complete"}, self.actor)
        self.assertEqual((revised.version, revised.state), (2, DraftState.READY))
        self.assertEqual(revised.preparation.validated_input.tags, [])

    def test_domain_errors_replace_preview(self):
        original = self.create()
        error = FieldError(("text",), "unavailable", "Недоступно")
        self.preview.side_effect = None
        self.preview.return_value = PreviewResult[NotePreview](errors=(error,))
        revised = self.manager.revise(original.draft_id, 1, {"text": "Other"}, self.actor)
        self.assertEqual(revised.state, DraftState.NEEDS_INPUT)
        self.assertIsNone(revised.preparation.preview)
        self.assertEqual(revised.preparation.errors, (error,))

    def test_stale_and_invalid_versions_do_not_run_preview(self):
        original = self.create()
        self.manager.revise(original.draft_id, 1, {"text": "Second"}, self.actor)
        self.preview.reset_mock()
        with self.assertRaises(DraftVersionConflictError):
            self.manager.revise(original.draft_id, 1, {"text": "Stale"}, self.actor)
        for version in (True, 0, "2"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    self.manager.revise(original.draft_id, version, {}, self.actor)
        self.preview.assert_not_called()
        self.assertEqual(self.store.get(original.draft_id, self.actor).payload["text"], "Second")

    def test_other_author_and_expiration_prevent_preview(self):
        original = self.create()
        self.preview.reset_mock()
        with self.assertRaises(PermissionDeniedError):
            self.manager.revise(original.draft_id, 1, {}, Actor("other"))
        self.clock.return_value = original.expires_at
        with self.assertRaises(DraftExpiredError):
            self.manager.revise(original.draft_id, 1, {}, self.actor)
        self.preview.assert_not_called()

    def test_revoked_permission_and_handler_failure_leave_snapshot_unchanged(self):
        original = self.create()
        self.authorizer.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.manager.revise(original.draft_id, 1, {"text": "Denied"}, self.actor)
        self.authorizer.is_allowed.return_value = True
        self.preview.side_effect = RuntimeError("Preview failed")
        with self.assertRaisesRegex(RuntimeError, "Preview failed"):
            self.manager.revise(original.draft_id, 1, {"text": "Failed"}, self.actor)
        self.assertEqual(self.store.get(original.draft_id, self.actor), original)

    def test_patch_replaces_fields_and_does_not_keep_caller_references(self):
        original = self.create()
        patch = {"tags": ["replacement"]}
        revised = self.manager.revise(original.draft_id, 1, patch, self.actor)
        patch["tags"].append("changed")
        revised.payload["tags"].append("changed returned snapshot")
        stored = self.store.get(original.draft_id, self.actor)
        self.assertEqual(stored.payload["tags"], ["replacement"])
        self.assertEqual(stored.preparation.validated_input.tags, ["replacement"])

    def test_expiration_during_preview_prevents_update(self):
        original = self.create()

        def expire(parameters, actor):
            self.clock.return_value = original.expires_at
            return self.build_preview(parameters, actor)

        self.preview.side_effect = expire
        with self.assertRaises(DraftExpiredError):
            self.manager.revise(original.draft_id, 1, {"text": "Late"}, self.actor)
        self.clock.return_value = self.now
        self.assertEqual(self.store.get(original.draft_id, self.actor), original)

    def test_concurrent_revisions_commit_one_version(self):
        original = self.create()
        barrier = Barrier(2)

        def preview(parameters, actor):
            barrier.wait(timeout=3)
            return self.build_preview(parameters, actor)

        def revise(text):
            try:
                return self.manager.revise(original.draft_id, 1, {"text": text}, self.actor)
            except DraftVersionConflictError as error:
                return error

        self.preview.side_effect = preview
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(revise, ("Second", "Third")))
        self.assertEqual(sum(isinstance(result, CommandDraft) for result in results), 1)
        self.assertEqual(sum(isinstance(result, DraftVersionConflictError) for result in results), 1)
        stored = self.store.get(original.draft_id, self.actor)
        self.assertEqual(stored.version, 2)
        self.assertIn(stored.payload["text"], ("Second", "Third"))

    def test_store_rejects_command_or_owner_change(self):
        original = self.create()
        for changes in ({"command_name": "other"}, {"command_version": 2}, {"actor_id": "other"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.store.update(
                        original.draft_id, 1, self.actor, payload=original.payload,
                        preparation=replace(original.preparation, **changes),
                    )
        self.assertEqual(self.store.get(original.draft_id, self.actor), original)

    def test_closed_snapshot_is_not_editable(self):
        original = self.create()
        for state in (DraftState.CANCELLED, DraftState.EXECUTED):
            with self.subTest(state=state):
                with self.assertRaises(DraftClosedError):
                    replace(original, state=state).require_editable(1)

    def test_invalid_patch_leaves_snapshot_unchanged(self):
        original = self.create()
        for patch in (None, [("text", "Other")], {1: "Other"}):
            with self.subTest(patch=patch):
                with self.assertRaises(TypeError):
                    self.manager.revise(original.draft_id, 1, patch, self.actor)
        self.assertEqual(self.store.get(original.draft_id, self.actor), original)

    def test_empty_patch_refreshes_preview_with_new_version(self):
        original = self.create()
        revised = self.manager.revise(original.draft_id, 1, {}, self.actor)
        self.assertEqual(revised.version, 2)
        self.assertEqual(revised.payload, original.payload)
        self.assertEqual(self.preview.call_count, 2)


if __name__ == "__main__":
    unittest.main()
