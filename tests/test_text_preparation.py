import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

from semantic_entry.adapters.memory.drafts import MemoryDraftStore
from semantic_entry.core.access import Actor, PermissionDeniedError
from semantic_entry.core.commands.catalog import CommandCatalog
from semantic_entry.core.commands.extraction import (
    CommandCandidate,
    ExtractionResult,
    ExtractionStatus,
)
from semantic_entry.core.commands.models import (
    CommandEffect,
    CommandModel,
    CommandSpec,
    PreviewResult,
)
from semantic_entry.core.commands.preparation import CommandPreparer
from semantic_entry.core.commands.queries import QueryExecutor
from semantic_entry.core.commands.registry import CommandRegistry
from semantic_entry.core.commands.text import CandidateNormalization
from semantic_entry.core.drafts.models import DraftState
from semantic_entry.core.engine import Engine
from semantic_entry.modules.warehouse.bootstrap import build_warehouse_engine
from semantic_entry.modules.warehouse.queries.receipt_resolution import ResolutionStatus


class WarehouseTextPreparationTest(unittest.TestCase):
    def setUp(self):
        self.factory = Mock(side_effect=AssertionError("Unexpected transaction"))
        self.drafts = Mock(wraps=MemoryDraftStore())
        self.policy = Mock()
        self.policy.is_allowed.return_value = True
        self.extractor = Mock()
        self.extractor.extract.return_value = ExtractionResult(
            status=ExtractionStatus.CANDIDATE,
            candidate=CommandCandidate(command_name="warehouse.record_receipt", command_version=1, parameters={}),
        )
        self.actor = Actor("alice")
        self.engine = build_warehouse_engine(self.factory, self.policy, drafts=self.drafts, extractor=self.extractor)

    def prepare(self, lifetime=timedelta(minutes=10)):
        return self.engine.prepare_from_text("Приход", self.actor, lifetime=lifetime)

    def tearDown(self):
        self.factory.assert_not_called()

    def test_partial_intent_creates_owned_draft_without_inventing_values(self):
        result = self.prepare()
        self.assertEqual(result.draft.state, DraftState.NEEDS_INPUT)
        self.assertEqual(result.draft.payload, {})
        self.assertEqual(result.draft.preparation.missing_fields, (("material_id",), ("supplier_id",), ("actual_date",), ("quantity_kg",)))
        self.assertEqual(result.resolution.material.status, ResolutionStatus.MISSING)
        self.assertEqual(result.resolution.supplier.status, ResolutionStatus.MISSING)
        self.assertEqual(self.engine.get_draft(result.draft.draft_id, self.actor), result.draft)
        self.drafts.create.assert_called_once()
        with self.assertRaises(PermissionDeniedError):
            self.engine.get_draft(result.draft.draft_id, Actor("other"))

    def test_noncommand_outcomes_do_not_create_drafts(self):
        for status in (ExtractionStatus.NEEDS_CLARIFICATION, ExtractionStatus.NO_MATCH, ExtractionStatus.MULTIPLE_COMMANDS):
            with self.subTest(status=status):
                self.extractor.extract.return_value = ExtractionResult(status=status, message="Уточните действие")
                result = self.prepare()
                self.assertEqual(result.extraction.status, status)
                self.assertIsNone(result.draft)
                self.assertIsNone(result.resolution)
        self.drafts.create.assert_not_called()

    def test_invalid_lifetime_is_rejected_before_extraction(self):
        for lifetime in (None, 1, timedelta(0), timedelta(seconds=-1)):
            with self.subTest(lifetime=lifetime), self.assertRaises((TypeError, ValueError)):
                self.prepare(lifetime)
        self.extractor.extract.assert_not_called()
        self.drafts.create.assert_not_called()

    def test_permission_denial_does_not_extract_or_create_draft(self):
        self.policy.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.prepare()
        self.extractor.extract.assert_not_called()
        self.drafts.create.assert_not_called()

    def test_provider_failure_does_not_create_draft(self):
        self.extractor.extract.side_effect = TimeoutError("Provider unavailable")
        with self.assertRaisesRegex(TimeoutError, "Provider unavailable"):
            self.prepare()
        self.drafts.create.assert_not_called()


class NoteInput(CommandModel):
    title: str


class NotePreview(CommandModel):
    title: str


class NoteResult(CommandModel):
    note_id: int


class GenericTextPreparationTest(unittest.TestCase):
    def setUp(self):
        self.spec = CommandSpec("notes.create", 1, CommandEffect.WRITE, NoteInput, NoteResult, "Create note", "notes.write")
        self.registry = CommandRegistry()
        self.registry.register(self.spec)
        self.policy = Mock()
        self.policy.is_allowed.return_value = True
        self.catalog = CommandCatalog(self.registry, self.policy)
        self.preparer = CommandPreparer(self.catalog)
        self.preview = Mock(side_effect=lambda parameters, actor: PreviewResult[NotePreview](preview=NotePreview(title=parameters.title)))
        self.preparer.register_preview(self.spec, NotePreview, self.preview)
        self.drafts = Mock(wraps=MemoryDraftStore())
        self.transactions = Mock(side_effect=AssertionError("Unexpected transaction"))
        self.write_handler = Mock(side_effect=AssertionError("Unexpected execution"))
        self.extractor = Mock()
        self.extractor.extract.return_value = ExtractionResult(status=ExtractionStatus.CANDIDATE, candidate=CommandCandidate(
            command_name="notes.create", command_version=1, parameters={"title": "Note"},
        ))
        self.engine = Engine(self.catalog, self.preparer, QueryExecutor(self.catalog), self.drafts, self.transactions, extractor=self.extractor)
        self.engine.register_write(self.spec, NotePreview, self.write_handler)
        self.engine.register_extraction(self.spec, NoteInput)
        self.actor = Actor("author")

    def prepare(self):
        return self.engine.prepare_from_text("Create note", self.actor, lifetime=timedelta(minutes=10))

    def tearDown(self):
        self.transactions.assert_not_called()
        self.write_handler.assert_not_called()

    def test_nonwarehouse_command_prepares_without_domain_normalizer(self):
        result = self.prepare()
        self.assertEqual(result.draft.state, DraftState.READY)
        self.assertEqual(result.draft.payload, {"title": "Note"})
        self.assertIsNone(result.resolution)
        self.assertEqual(self.engine.get_draft(result.draft.draft_id, self.actor), result.draft)

    def test_normalizer_keeps_original_extraction_and_returns_resolution_snapshot(self):
        resolution = NotePreview(title="Resolved")

        def normalize(parameters, actor):
            self.assertEqual(actor, self.actor)
            parameters["title"] = "Normalized"
            return CandidateNormalization(parameters, resolution)

        self.engine.register_normalizer(self.spec, normalize)
        result = self.prepare()
        resolution.title = "Changed"
        self.assertEqual(result.extraction.candidate.parameters, {"title": "Note"})
        self.assertEqual(result.draft.payload, {"title": "Normalized"})
        self.assertEqual(result.draft.preparation.preview.title, "Normalized")
        self.assertEqual(result.resolution.title, "Resolved")

    def test_conflicting_spec_is_rejected_before_normalization(self):
        normalize = Mock()
        self.engine.register_normalizer(replace(self.spec, permission="other.permission"), normalize)
        with self.assertRaisesRegex(ValueError, "спецификации"):
            self.prepare()
        normalize.assert_not_called()
        self.drafts.create.assert_not_called()

    def test_revocation_during_normalization_prevents_preview_and_draft(self):
        def normalize(parameters, actor):
            self.policy.is_allowed.return_value = False
            return CandidateNormalization(parameters)

        self.engine.register_normalizer(self.spec, normalize)
        with self.assertRaises(PermissionDeniedError):
            self.prepare()
        self.preview.assert_not_called()
        self.drafts.create.assert_not_called()

    def test_invalid_normalization_result_is_rejected_before_draft(self):
        normalize = Mock()
        self.engine.register_normalizer(self.spec, normalize)
        for result in ({"title": "Note"}, CandidateNormalization({"title": "Note"}, resolution="invalid")):
            with self.subTest(result=result):
                normalize.return_value = result
                with self.assertRaises(TypeError):
                    self.prepare()
        self.drafts.create.assert_not_called()

    def test_normalization_failure_propagates_without_draft(self):
        self.engine.register_normalizer(self.spec, Mock(side_effect=RuntimeError("Lookup failed")))
        with self.assertRaisesRegex(RuntimeError, "Lookup failed"):
            self.prepare()
        self.drafts.create.assert_not_called()

    def test_read_command_cannot_be_prepared_or_normalized(self):
        read = replace(self.spec, name="notes.list", effect=CommandEffect.READ)
        self.registry.register(read)
        self.engine.register_extraction(read, NoteInput)
        normalize = Mock()
        with self.assertRaisesRegex(ValueError, "WRITE"):
            self.engine.register_normalizer(read, normalize)
        self.extractor.extract.return_value.candidate.command_name = read.name
        with self.assertRaisesRegex(ValueError, "WRITE"):
            self.prepare()
        normalize.assert_not_called()
        self.drafts.create.assert_not_called()

    def test_invalid_and_duplicate_registration_preserve_original_handler(self):
        normalize = Mock(return_value=CandidateNormalization({"title": "Normalized"}))
        self.engine.register_normalizer(self.spec, normalize)
        with self.assertRaises(ValueError):
            self.engine.register_normalizer(self.spec, Mock())
        with self.assertRaises(TypeError):
            self.engine.register_normalizer(None, normalize)
        with self.assertRaises(TypeError):
            self.engine.register_normalizer(replace(self.spec, version=2), None)
        self.assertEqual(self.prepare().draft.payload, {"title": "Normalized"})
        normalize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
