import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from pydantic import Field, StrictInt

from semantic_entry.core.access import Actor, PermissionDeniedError
from semantic_entry.core.commands.catalog import CommandCatalog
from semantic_entry.core.commands.models import CommandEffect, CommandModel, CommandSpec
from semantic_entry.core.commands.preparation import CommandPreparer
from semantic_entry.core.commands.registry import CommandRegistry


class ItemInput(CommandModel):
    value: StrictInt = Field(gt=0)


class BatchInput(CommandModel):
    title: str
    items: list[ItemInput]
    copies: StrictInt = 1


class BatchResult(CommandModel):
    count: StrictInt


class CommandPreparationTest(unittest.TestCase):
    def setUp(self):
        self.actor = Actor("operator")
        self.spec = CommandSpec(
            "batch.create", 1, CommandEffect.WRITE, BatchInput, BatchResult,
            "Создать набор", "batch.write",
        )
        self.registry = CommandRegistry()
        self.registry.register(self.spec)
        self.authorizer = Mock()
        self.authorizer.is_allowed.return_value = True
        self.preparer = CommandPreparer(CommandCatalog(self.registry, self.authorizer))

    def prepare(self, payload, version=1):
        return self.preparer.prepare(self.spec.name, version, payload, self.actor)

    def test_valid_input_returns_typed_values_defaults_and_trusted_identity(self):
        payload = {"title": "First", "items": [{"value": 2}]}
        result = self.prepare(payload)
        self.assertTrue(result.is_valid)
        self.assertEqual((result.command_name, result.command_version), ("batch.create", 1))
        self.assertEqual(result.actor_id, "operator")
        self.assertIsInstance(result.validated_input, BatchInput)
        self.assertEqual(result.validated_input.copies, 1)
        self.assertEqual(result.validated_input.items[0].value, 2)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.missing_fields, ())
        self.assertEqual(payload, {"title": "First", "items": [{"value": 2}]})
        self.authorizer.is_allowed.assert_called_once_with(self.actor, "batch.write")

    def test_missing_fields_exclude_fields_with_defaults(self):
        result = self.prepare({})
        self.assertFalse(result.is_valid)
        self.assertIsNone(result.validated_input)
        self.assertEqual(result.missing_fields, (("title",), ("items",)))
        self.assertTrue(all(error.code == "missing" for error in result.errors))
        self.assertTrue(all(error.message for error in result.errors))

    def test_nested_errors_keep_list_indices_and_distinguish_missing_from_invalid(self):
        result = self.prepare({
            "title": "First", "items": [{}, {"value": -1}, {"value": "2"}], "extra": True,
        })
        self.assertFalse(result.is_valid)
        self.assertIsNone(result.validated_input)
        self.assertEqual(
            [(error.path, error.code) for error in result.errors],
            [
                (("items", 0, "value"), "missing"),
                (("items", 1, "value"), "greater_than"),
                (("items", 2, "value"), "int_type"),
                (("extra",), "extra_forbidden"),
            ],
        )
        self.assertEqual(result.missing_fields, (("items", 0, "value"),))

    def test_root_payload_must_be_mapping_with_string_keys(self):
        for payload in (None, "text", [], [("title", "First")], {1: "First"}):
            with self.subTest(payload=payload):
                result = self.prepare(payload)
                self.assertFalse(result.is_valid)
                self.assertIsNone(result.validated_input)
                self.assertEqual([(error.path, error.code) for error in result.errors], [((), "mapping_type")])
                self.assertEqual(result.missing_fields, ())

    def test_denied_access_precedes_payload_validation_for_read_and_write(self):
        self.authorizer.is_allowed.return_value = False
        self.registry.register(replace(self.spec, version=2, effect=CommandEffect.READ))
        with patch.object(BatchInput, "model_validate") as validate:
            for version in (1, 2):
                for payload in (None, {"title": "First", "items": []}):
                    with self.subTest(version=version, payload=payload):
                        with self.assertRaises(PermissionDeniedError):
                            self.prepare(payload, version)
            validate.assert_not_called()

    def test_authorization_failure_is_not_reported_as_invalid_input(self):
        self.authorizer.is_allowed.side_effect = RuntimeError("Policy unavailable")
        with self.assertRaisesRegex(RuntimeError, "Policy unavailable"):
            self.prepare({})

    def test_authorization_is_checked_again_on_each_preparation(self):
        payload = {"title": "First", "items": []}
        self.assertTrue(self.prepare(payload).is_valid)
        self.authorizer.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.prepare(payload)

    def test_requested_version_selects_its_own_input_schema(self):
        class RevisedBatchInput(BatchInput):
            label: str

        self.registry.register(replace(self.spec, version=2, input_model=RevisedBatchInput))
        payload = {"title": "First", "items": []}
        self.assertTrue(self.prepare(payload, 1).is_valid)
        result = self.prepare(payload, 2)
        self.assertEqual(result.command_version, 2)
        self.assertEqual(result.missing_fields, (("label",),))
        for name, version in ((self.spec.name, 3), ("unknown", 1)):
            with self.subTest(name=name, version=version):
                with self.assertRaises(KeyError):
                    self.preparer.prepare(name, version, payload, self.actor)

    def test_payload_cannot_supply_actor_identity(self):
        result = self.prepare({"title": "First", "items": [], "actor_id": "administrator"})
        self.assertFalse(result.is_valid)
        self.assertEqual(result.actor_id, "operator")
        self.assertEqual([(error.path, error.code) for error in result.errors], [(("actor_id",), "extra_forbidden")])

    def test_internal_validation_failure_propagates(self):
        with patch.object(BatchInput, "model_validate", side_effect=RuntimeError("Broken validator")):
            with self.assertRaisesRegex(RuntimeError, "Broken validator"):
                self.prepare({"title": "First", "items": []})


if __name__ == "__main__":
    unittest.main()
