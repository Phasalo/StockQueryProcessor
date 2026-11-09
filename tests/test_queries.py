import unittest
from dataclasses import replace
from unittest.mock import Mock

from pydantic import StrictInt, ValidationError

from semantic_entry.core import (
    Actor,
    CommandCatalog,
    CommandEffect,
    CommandModel,
    CommandRegistry,
    CommandSpec,
    PermissionDeniedError,
    QueryExecutor,
)


class AddInput(CommandModel):
    left: StrictInt
    right: StrictInt = 0


class AddResult(CommandModel):
    value: StrictInt


class QueryExecutorTest(unittest.TestCase):
    def setUp(self):
        self.actor = Actor("reader")
        self.spec = CommandSpec(
            "calculator.add", 1, CommandEffect.READ, AddInput, AddResult,
            "Сложить числа", "calculator.read",
        )
        self.registry = CommandRegistry()
        self.registry.register(self.spec)
        self.authorizer = Mock()
        self.authorizer.is_allowed.return_value = True
        self.executor = QueryExecutor(CommandCatalog(self.registry, self.authorizer))
        self.handler = Mock(side_effect=lambda parameters, actor: AddResult(value=parameters.left + parameters.right))
        self.executor.register(self.spec, self.handler)

    def query(self, payload, version=1):
        return self.executor.query(self.spec.name, version, payload, self.actor)

    def test_handler_receives_typed_parameters_defaults_and_trusted_actor(self):
        result = self.query({"left": 3})
        self.assertIsInstance(result, AddResult)
        self.assertEqual(result.value, 3)
        self.handler.assert_called_once_with(AddInput(left=3, right=0), self.actor)
        self.authorizer.is_allowed.assert_called_once_with(self.actor, "calculator.read")

    def test_validation_failure_does_not_call_handler(self):
        for payload in ({}, {"left": True}, {"left": 1, "actor_id": "admin"}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.query(payload)
        self.handler.assert_not_called()

    def test_root_payload_is_not_coerced_into_mapping(self):
        for payload in (None, [("left", 1)], {1: "left"}):
            with self.subTest(payload=payload):
                with self.assertRaises(TypeError):
                    self.query(payload)
        self.handler.assert_not_called()

    def test_denied_access_precedes_validation_and_handler_lookup(self):
        self.authorizer.is_allowed.return_value = False
        self.registry.register(replace(self.spec, version=2))
        for version in (1, 2):
            with self.subTest(version=version):
                with self.assertRaises(PermissionDeniedError):
                    self.query(None, version)
        self.handler.assert_not_called()

    def test_access_is_checked_again_after_revocation(self):
        self.query({"left": 1})
        self.authorizer.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.query({"left": 2})
        self.assertEqual(self.handler.call_count, 1)

    def test_write_commands_are_rejected_at_registration_and_execution(self):
        write = replace(self.spec, version=2, effect=CommandEffect.WRITE)
        self.registry.register(write)
        with self.assertRaisesRegex(ValueError, "READ"):
            self.executor.register(write, self.handler)
        with self.assertRaisesRegex(ValueError, "READ"):
            self.query({"left": 1}, 2)
        self.handler.assert_not_called()

    def test_duplicate_registration_keeps_original_handler(self):
        replacement = Mock()
        with self.assertRaisesRegex(ValueError, "уже зарегистрирован"):
            self.executor.register(self.spec, replacement)
        self.assertEqual(self.query({"left": 2}).value, 2)
        replacement.assert_not_called()

    def test_versions_use_separate_handlers_without_fallback(self):
        second = replace(self.spec, version=2)
        self.registry.register(second)
        with self.assertRaises(KeyError):
            self.query({"left": 1}, 2)
        handler = Mock(return_value=AddResult(value=100))
        self.executor.register(second, handler)
        self.assertEqual(self.query({"left": 1}, 2).value, 100)
        self.handler.assert_not_called()
        with self.assertRaises(KeyError):
            self.query({"left": 1}, 3)
        with self.assertRaises(KeyError):
            self.executor.query("missing", 1, {"left": 1}, self.actor)

    def test_handler_cannot_be_bound_to_conflicting_metadata(self):
        executor = QueryExecutor(CommandCatalog(self.registry, self.authorizer))
        executor.register(replace(self.spec, permission="other.permission"), self.handler)
        with self.assertRaisesRegex(ValueError, "спецификации"):
            executor.query(self.spec.name, 1, {"left": 1}, self.actor)
        self.handler.assert_not_called()

    def test_return_value_must_use_declared_result_model(self):
        for result in ({"value": 1}, AddInput(left=1), None):
            with self.subTest(result=result):
                self.handler.side_effect = None
                self.handler.return_value = result
                with self.assertRaises(TypeError):
                    self.query({"left": 1})

    def test_result_is_revalidated_after_handler_mutation(self):
        result = AddResult(value=1)
        result.value = "broken"
        self.handler.side_effect = None
        self.handler.return_value = result
        with self.assertWarns(UserWarning), self.assertRaises(ValidationError):
            self.query({"left": 1})

    def test_policy_and_handler_errors_propagate(self):
        self.authorizer.is_allowed.side_effect = RuntimeError("Policy unavailable")
        with self.assertRaisesRegex(RuntimeError, "Policy unavailable"):
            self.query({"left": 1})
        self.handler.assert_not_called()
        self.authorizer.is_allowed.side_effect = None
        self.handler.side_effect = RuntimeError("Query failed")
        with self.assertRaisesRegex(RuntimeError, "Query failed"):
            self.query({"left": 1})

    def test_invalid_registration_is_rejected(self):
        with self.assertRaises(TypeError):
            self.executor.register(self.spec, None)
        with self.assertRaises(TypeError):
            self.executor.register(None, self.handler)


if __name__ == "__main__":
    unittest.main()
