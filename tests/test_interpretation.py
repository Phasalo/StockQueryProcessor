import unittest
from dataclasses import replace
from unittest.mock import Mock

from pydantic import ValidationError

from semantic_entry.core.access import Actor, PermissionDeniedError
from semantic_entry.core.commands.catalog import CommandCatalog
from semantic_entry.core.commands.extraction import (
    CommandCandidate,
    ExtractionContractError,
    ExtractionResult,
    ExtractionStatus,
)
from semantic_entry.core.commands.interpretation import CommandInterpreter
from semantic_entry.core.commands.models import CommandEffect, CommandModel, CommandSpec
from semantic_entry.core.commands.registry import CommandRegistry
from semantic_entry.core.ports.drafts import DraftStore
from semantic_entry.modules.warehouse.bootstrap import build_warehouse_engine


class NoteInput(CommandModel):
    title: str


class NoteResult(CommandModel):
    note_id: int


class CommandInterpretationTest(unittest.TestCase):
    def setUp(self):
        self.factory = Mock(side_effect=AssertionError("Unexpected transaction"))
        self.drafts = Mock(spec=DraftStore)
        self.policy = Mock()
        self.policy.is_allowed.return_value = True
        self.extractor = Mock()
        self.actor = Actor("alice")
        self.engine = build_warehouse_engine(self.factory, self.policy, drafts=self.drafts, extractor=self.extractor)
        self.extractor.extract.return_value = self.result({
            "material": {"film_type_code": "BOPP", "format": 1200, "thickness": 25},
            "supplier_name": "Acme", "quantity_kg": "12.50", "actual_date": "2026-11-10",
        })

    @staticmethod
    def result(parameters, *, name="warehouse.record_receipt", version=1):
        return ExtractionResult(status=ExtractionStatus.CANDIDATE, candidate=CommandCandidate(
            command_name=name, command_version=version, parameters=parameters,
        ))

    def tearDown(self):
        self.factory.assert_not_called()
        self.drafts.create.assert_not_called()

    def test_interpretation_receives_only_registered_allowed_commands(self):
        text = "  Пришло 12.50 кг BOPP 1200 на 25 от Acme 10 ноября 2026  "
        result = self.engine.interpret(text, self.actor)
        sent_text, commands = self.extractor.extract.call_args.args
        self.assertEqual(sent_text, text)
        self.assertEqual([(command.name, command.version) for command in commands], [("warehouse.record_receipt", 1)])
        self.assertEqual(commands[0].effect, CommandEffect.WRITE)
        self.assertIn("material", commands[0].input_schema["properties"])
        self.assertNotIn("material_id", commands[0].input_schema["properties"])
        self.assertNotIn("supplier_id", commands[0].input_schema["properties"])
        self.assertEqual(result.candidate.parameters["quantity_kg"], "12.50")

    def test_omitted_values_are_not_invented(self):
        self.extractor.extract.return_value = self.result({"supplier_name": "Acme"})
        result = self.engine.interpret("Приход от Acme", self.actor)
        self.assertEqual(result.candidate.parameters, {"supplier_name": "Acme"})

    def test_unknown_command_or_version_is_rejected(self):
        for name, version in (("warehouse.get_stock", 1), ("warehouse.record_receipt", 2), ("system.execute_sql", 1)):
            with self.subTest(name=name, version=version):
                self.extractor.extract.return_value = self.result({}, name=name, version=version)
                with self.assertRaises(ExtractionContractError):
                    self.engine.interpret("Текст", self.actor)

    def test_invalid_values_and_invented_identifiers_are_rejected(self):
        for parameters in (
            {"quantity_kg": "-1"},
            {"quantity_kg": "NaN"},
            {"quantity_kg": "1.001"},
            {"quantity_kg": "100000000"},
            {"actual_date": "2026-02-30"},
            {"material": {"format": True}},
            {"material": {"format": "1200"}},
            {"material_id": 1},
            {"supplier_id": 1},
            {"confirmed": True},
        ):
            with self.subTest(parameters=parameters):
                self.extractor.extract.return_value = self.result(parameters)
                with self.assertRaises(ExtractionContractError):
                    self.engine.interpret("Текст", self.actor)

    def test_denied_access_does_not_call_extractor(self):
        self.policy.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.engine.interpret("Приход", self.actor)
        self.extractor.extract.assert_not_called()

    def test_permission_is_rechecked_after_extraction(self):
        extracted = self.extractor.extract.return_value

        def extract(text, commands):
            self.policy.is_allowed.return_value = False
            return extracted

        self.extractor.extract.side_effect = extract
        with self.assertRaises(PermissionDeniedError):
            self.engine.interpret("Приход", self.actor)

    def test_invalid_text_does_not_call_extractor(self):
        for text in (None, 1, {}, "", " \n\t "):
            with self.subTest(text=text), self.assertRaises((TypeError, ValueError)):
                self.engine.interpret(text, self.actor)
        self.extractor.extract.assert_not_called()

    def test_noncommand_outcomes_do_not_create_candidates(self):
        for status in (ExtractionStatus.NEEDS_CLARIFICATION, ExtractionStatus.NO_MATCH, ExtractionStatus.MULTIPLE_COMMANDS):
            with self.subTest(status=status):
                self.extractor.extract.return_value = ExtractionResult(status=status, message="Уточните одно действие")
                result = self.engine.interpret("Текст", self.actor)
                self.assertEqual(result.status, status)
                self.assertIsNone(result.candidate)

    def test_provider_failure_propagates_without_side_effects(self):
        self.extractor.extract.side_effect = TimeoutError("Provider unavailable")
        with self.assertRaisesRegex(TimeoutError, "Provider unavailable"):
            self.engine.interpret("Приход", self.actor)

    def test_untyped_provider_response_is_rejected(self):
        self.extractor.extract.return_value = {"status": "candidate"}
        with self.assertRaises(ExtractionContractError):
            self.engine.interpret("Приход", self.actor)

    def test_mutated_provider_result_is_revalidated(self):
        result = self.extractor.extract.return_value
        result.candidate = None
        with self.assertRaises(ExtractionContractError):
            self.engine.interpret("Приход", self.actor)

    def test_provider_cannot_modify_registered_schema(self):
        result = self.extractor.extract.return_value

        def extract(text, commands):
            commands[0].input_schema["properties"].clear()
            return result

        self.extractor.extract.side_effect = extract
        self.engine.interpret("Приход", self.actor)
        self.extractor.extract.side_effect = None
        self.engine.interpret("Приход", self.actor)
        commands = self.extractor.extract.call_args.args[1]
        self.assertIn("material", commands[0].input_schema["properties"])

    def test_structured_queries_work_without_extractor(self):
        engine = build_warehouse_engine(self.factory, self.policy, drafts=self.drafts)
        result = engine.query("warehouse.resolve_receipt", 1, {}, self.actor)
        self.assertEqual(result.resolved_ids, {})
        with self.assertRaisesRegex(RuntimeError, "не настроено"):
            engine.interpret("Приход", self.actor)


class GenericCommandInterpreterTest(unittest.TestCase):
    def setUp(self):
        self.spec = CommandSpec("notes.create", 1, CommandEffect.WRITE, NoteInput, NoteResult, "Create note", "notes.write")
        self.registry = CommandRegistry()
        self.registry.register(self.spec)
        self.policy = Mock()
        self.policy.is_allowed.return_value = True
        self.extractor = Mock()
        self.extractor.extract.return_value = ExtractionResult(status=ExtractionStatus.CANDIDATE, candidate=CommandCandidate(
            command_name="notes.create", command_version=1, parameters={"title": "Note"},
        ))
        self.interpreter = CommandInterpreter(CommandCatalog(self.registry, self.policy), self.extractor)
        self.interpreter.register(self.spec, NoteInput)
        self.actor = Actor("author")

    def test_nonwarehouse_module_uses_its_own_extraction_schema(self):
        result = self.interpreter.interpret("Create note", self.actor)
        self.assertEqual(result.candidate.parameters, {"title": "Note"})
        command = self.extractor.extract.call_args.args[1][0]
        self.assertEqual(command.input_schema, NoteInput.model_json_schema())
        self.assertEqual(command.name, "notes.create")

    def test_versions_use_registered_extraction_schema(self):
        second = replace(self.spec, version=2)
        self.registry.register(second)
        self.extractor.extract.return_value.candidate.command_version = 2
        with self.assertRaises(ExtractionContractError):
            self.interpreter.interpret("Create note", self.actor)
        self.interpreter.register(second, NoteInput)
        self.assertEqual(self.interpreter.interpret("Create note", self.actor).candidate.command_version, 2)

    def test_conflicting_spec_is_rejected_before_provider_call(self):
        interpreter = CommandInterpreter(CommandCatalog(self.registry, self.policy), self.extractor)
        interpreter.register(replace(self.spec, permission="other.permission"), NoteInput)
        with self.assertRaisesRegex(ValueError, "спецификации"):
            interpreter.interpret("Create note", self.actor)
        self.extractor.extract.assert_not_called()

    def test_invalid_and_duplicate_registration_are_rejected(self):
        with self.assertRaises(ValueError):
            self.interpreter.register(self.spec, NoteInput)
        with self.assertRaises(TypeError):
            self.interpreter.register(None, NoteInput)
        with self.assertRaises(TypeError):
            self.interpreter.register(replace(self.spec, version=2), dict)

    def test_missing_required_extraction_field_is_rejected(self):
        self.extractor.extract.return_value.candidate.parameters = {}
        with self.assertRaises(ExtractionContractError):
            self.interpreter.interpret("Create note", self.actor)

    def test_outcome_shape_rejects_missing_candidate_and_empty_clarification(self):
        for payload in (
            {"status": "candidate"},
            {"status": "needs_clarification"},
            {"status": "needs_clarification", "message": " "},
            {"status": "multiple_commands", "candidate": {"command_name": "notes.create", "command_version": 1, "parameters": {}}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ExtractionResult.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
