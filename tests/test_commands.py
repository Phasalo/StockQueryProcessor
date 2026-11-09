import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError, replace

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from semantic_entry.core import (
    CommandEffect,
    CommandModel,
    CommandRegistry,
    CommandSpec,
)


class AddInput(CommandModel):
    left: StrictInt = Field(ge=0)
    right: StrictInt


class AddResult(CommandModel):
    value: StrictInt


class CommandContractsTest(unittest.TestCase):
    def setUp(self):
        self.spec = CommandSpec(
            name="calculator.add", version=1, effect=CommandEffect.READ,
            input_model=AddInput, result_model=AddResult,
            description="Сложить два числа", permission="calculator.read",
        )

    def test_registry_distinguishes_versions_and_effects(self):
        registry = CommandRegistry()
        updated = replace(self.spec, version=2, effect=CommandEffect.WRITE, permission="calculator.write")
        registry.register(self.spec)
        registry.register(updated)
        self.assertEqual(self.spec.key, "calculator.add@1")
        self.assertIs(registry.get("calculator.add", 1), self.spec)
        self.assertIs(registry.get("calculator.add", 2), updated)
        self.assertEqual(registry.list(), (self.spec, updated))
        self.assertEqual(CommandRegistry().list(), ())

    def test_duplicate_registration_does_not_replace_original(self):
        registry = CommandRegistry()
        registry.register(self.spec)
        with self.assertRaisesRegex(ValueError, "уже зарегистрирована"):
            registry.register(replace(self.spec, description="Другое описание"))
        self.assertIs(registry.get(self.spec.name, self.spec.version), self.spec)

    def test_lookup_does_not_fall_back_to_another_command_or_version(self):
        registry = CommandRegistry()
        registry.register(self.spec)
        for name, version in (("missing", 1), (self.spec.name, 2)):
            with self.subTest(name=name, version=version):
                with self.assertRaises(KeyError):
                    registry.get(name, version)
        for version in (True, 0, "1"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    registry.get(self.spec.name, version)

    def test_input_validation_preserves_schema_errors(self):
        value = self.spec.validate_input({"left": 2, "right": 3})
        self.assertIsInstance(value, AddInput)
        self.assertEqual((value.left, value.right), (2, 3))
        cases = (
            ({"left": 2}, "right", "missing"),
            ({"left": -1, "right": 3}, "left", "greater_than_equal"),
            ({"left": True, "right": 3}, "left", "int_type"),
            ({"left": "2", "right": 3}, "left", "int_type"),
            ({"left": 2, "right": 3, "extra": 1}, "extra", "extra_forbidden"),
        )
        for payload, field, code in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError) as raised:
                    self.spec.validate_input(payload)
                error = raised.exception.errors()[0]
                self.assertEqual(error["loc"], (field,))
                self.assertEqual(error["type"], code)

    def test_result_validation_uses_result_model(self):
        result = self.spec.validate_result({"value": 5})
        self.assertIsInstance(result, AddResult)
        self.assertEqual(result.value, 5)
        for payload in ({"value": True}, {"left": 2, "right": 3}, {"value": 5, "extra": 1}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    self.spec.validate_result(payload)

    def test_json_schema_exposes_validation_constraints_and_extra_policy(self):
        schema = self.spec.input_json_schema()
        self.assertEqual(schema["required"], ["left", "right"])
        self.assertEqual(schema["properties"]["left"]["minimum"], 0)
        self.assertEqual(schema["properties"]["left"]["type"], "integer")
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(self.spec.result_json_schema()["required"], ["value"])

    def test_invalid_metadata_is_rejected(self):
        cases = (
            {"name": ""}, {"name": "bad name"}, {"name": "name@1"},
            {"version": 0}, {"version": True}, {"version": "1"},
            {"description": "   "}, {"permission": ""},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(self.spec, **changes)
        with self.assertRaises(TypeError):
            replace(self.spec, effect="READ")

    def test_model_contract_cannot_be_replaced_with_unvalidated_or_permissive_data(self):
        class PermissiveInput(CommandModel):
            model_config = ConfigDict(extra="ignore")

        for field in ("input_model", "result_model"):
            for model in (dict, BaseModel, AddInput(left=1, right=2)):
                with self.subTest(field=field, model=model):
                    with self.assertRaises(TypeError):
                        replace(self.spec, **{field: model})
            with self.assertRaises(ValueError):
                replace(self.spec, **{field: PermissiveInput})

    def test_registered_metadata_cannot_be_reassigned(self):
        registry = CommandRegistry()
        registry.register(self.spec)
        with self.assertRaises(FrozenInstanceError):
            self.spec.version = 2
        with self.assertRaises(TypeError):
            registry.register(lambda: None)
        self.assertIs(registry.get(self.spec.name, 1), self.spec)

    def test_core_import_and_registration_do_not_depend_on_warehouse_or_infrastructure(self):
        code = '''
import builtins
original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in {'database', 'config', 'app', 'psycopg2', 'flask'}:
        raise AssertionError('Unexpected dependency: ' + name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
from semantic_entry.core import CommandEffect, CommandModel, CommandRegistry, CommandSpec
class Input(CommandModel):
    text: str
class Result(CommandModel):
    text: str
registry = CommandRegistry()
spec = CommandSpec('echo', 1, CommandEffect.READ, Input, Result, 'Repeat text', 'echo.read')
registry.register(spec)
assert registry.get('echo', 1).validate_input({'text': 'hello'}).text == 'hello'
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
