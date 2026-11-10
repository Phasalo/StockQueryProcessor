import unittest
from dataclasses import replace
from unittest.mock import Mock

from pydantic import StrictInt, ValidationError

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


class AddInput(CommandModel):
    left: StrictInt
    right: StrictInt = 0


class AddPreview(CommandModel):
    total: StrictInt


class PreviewPreparationTest(unittest.TestCase):
    def setUp(self):
        self.spec = CommandSpec(
            "calculator.save_sum", 1, CommandEffect.WRITE, AddInput, AddPreview,
            "Сохранить сумму", "calculator.write",
        )
        self.registry = CommandRegistry()
        self.registry.register(self.spec)
        self.authorizer = Mock()
        self.authorizer.is_allowed.return_value = True
        self.preparer = CommandPreparer(CommandCatalog(self.registry, self.authorizer))
        self.actor = Actor("operator")
        self.handler = Mock(return_value=PreviewResult[AddPreview](preview=AddPreview(total=5)))
        self.preparer.register_preview(self.spec, AddPreview, self.handler)

    def prepare(self, payload, version=1):
        return self.preparer.prepare(self.spec.name, version, payload, self.actor)

    def test_prepare_passes_typed_input_and_actor_to_preview_handler(self):
        result = self.prepare({"left": 5})
        self.assertTrue(result.is_valid)
        self.assertEqual((result.command_name, result.command_version, result.actor_id), (self.spec.name, 1, "operator"))
        self.assertIsInstance(result.preview, AddPreview)
        self.assertEqual(result.preview.total, 5)
        self.assertEqual(result.errors, ())
        self.handler.assert_called_once_with(AddInput(left=5, right=0), self.actor)
        self.authorizer.is_allowed.assert_called_once_with(self.actor, "calculator.write")

    def test_invalid_input_does_not_call_preview_handler(self):
        for payload in ({}, {"left": "5"}, {"left": 5, "extra": 1}, None):
            with self.subTest(payload=payload):
                result = self.prepare(payload)
                self.assertFalse(result.is_valid)
                self.assertIsNone(result.preview)
        self.handler.assert_not_called()

    def test_denied_access_does_not_call_preview_handler(self):
        self.authorizer.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.prepare({})
        self.handler.assert_not_called()

    def test_revoked_permission_is_checked_again(self):
        self.assertTrue(self.prepare({"left": 5}).is_valid)
        self.authorizer.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.prepare({"left": 6})
        self.assertEqual(self.handler.call_count, 1)

    def test_domain_errors_keep_validated_input_for_correction(self):
        error = FieldError(("left",), "not_allowed", "Значение недоступно.")
        self.handler.return_value = PreviewResult[AddPreview](errors=(error,))
        result = self.prepare({"left": 5})
        self.assertFalse(result.is_valid)
        self.assertEqual(result.validated_input.left, 5)
        self.assertIsNone(result.preview)
        self.assertEqual(result.errors, (error,))
        self.assertEqual(result.missing_fields, ())

    def test_handler_failure_propagates(self):
        self.handler.side_effect = RuntimeError("Preview unavailable")
        with self.assertRaisesRegex(RuntimeError, "Preview unavailable"):
            self.prepare({"left": 5})

    def test_preview_result_must_use_registered_model(self):
        for value in (None, {}, AddPreview(total=5), PreviewResult[AddInput](preview=AddInput(left=5))):
            with self.subTest(value=value):
                self.handler.return_value = value
                with self.assertRaises(TypeError):
                    self.prepare({"left": 5})

    def test_preview_requires_either_card_or_errors(self):
        for result in (
            PreviewResult[AddPreview](),
            PreviewResult[AddPreview](preview=AddPreview(total=5), errors=(FieldError((), "invalid", "Ошибка"),)),
        ):
            with self.subTest(result=result):
                self.handler.return_value = result
                with self.assertRaisesRegex(ValueError, "карточку либо ошибки"):
                    self.prepare({"left": 5})

    def test_mutated_preview_is_revalidated(self):
        self.handler.return_value.preview.total = "invalid"
        with self.assertWarns(UserWarning), self.assertRaises(ValidationError):
            self.prepare({"left": 5})

    def test_duplicate_registration_keeps_original_handler(self):
        replacement = Mock()
        with self.assertRaisesRegex(ValueError, "уже зарегистрирован"):
            self.preparer.register_preview(self.spec, AddPreview, replacement)
        self.assertEqual(self.prepare({"left": 5}).preview.total, 5)
        replacement.assert_not_called()

    def test_preview_does_not_fall_back_to_another_version(self):
        second = replace(self.spec, version=2)
        self.registry.register(second)
        result = self.prepare({"left": 5}, 2)
        self.assertTrue(result.is_valid)
        self.assertIsNone(result.preview)
        self.handler.assert_not_called()
        with self.assertRaises(KeyError):
            self.prepare({"left": 5}, 3)

    def test_conflicting_specification_is_rejected(self):
        preparer = CommandPreparer(CommandCatalog(self.registry, self.authorizer))
        preparer.register_preview(replace(self.spec, permission="other"), AddPreview, self.handler)
        with self.assertRaisesRegex(ValueError, "другой спецификации"):
            preparer.prepare(self.spec.name, 1, {"left": 5}, self.actor)
        self.handler.assert_not_called()

    def test_invalid_registrations_are_rejected(self):
        with self.assertRaises(ValueError):
            self.preparer.register_preview(replace(self.spec, effect=CommandEffect.READ), AddPreview, self.handler)
        with self.assertRaises(TypeError):
            self.preparer.register_preview(self.spec, dict, self.handler)
        with self.assertRaises(TypeError):
            self.preparer.register_preview(self.spec, AddPreview, None)


if __name__ == "__main__":
    unittest.main()
