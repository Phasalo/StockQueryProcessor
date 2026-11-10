import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from config import WarehouseConfig
from semantic_entry.core import (
    Actor,
    CommandCatalog,
    CommandPreparer,
    CommandRegistry,
    PermissionDeniedError,
    QueryExecutor,
)
from semantic_entry.modules.warehouse.postgres.handlers.stock import StockQueryHandler
from semantic_entry.modules.warehouse.queries import GET_STOCK_SPEC, GetStockInput


class WarehouseStockQueryTest(unittest.TestCase):
    def setUp(self):
        self.registry = CommandRegistry()
        self.registry.register(GET_STOCK_SPEC)
        self.authorizer = Mock()
        self.authorizer.is_allowed.return_value = True
        self.catalog = CommandCatalog(self.registry, self.authorizer)
        self.executor = QueryExecutor(self.catalog)
        self.executor.register(GET_STOCK_SPEC, StockQueryHandler(WarehouseConfig()))
        self.actor = Actor("stock-reader")

    def test_invalid_material_id_is_rejected_before_connection(self):
        with patch("semantic_entry.modules.warehouse.postgres.unit_of_work.create_connection") as connect:
            for payload in ({}, {"material_id": True}, {"material_id": 0}, {"material_id": -1}, {"material_id": "1"}, {"material_id": 1, "extra": 1}):
                with self.subTest(payload=payload):
                    with self.assertRaises(ValidationError):
                        self.executor.query("warehouse.get_stock", 1, payload, self.actor)
            connect.assert_not_called()

    def test_permission_is_checked_before_connection(self):
        self.authorizer.is_allowed.return_value = False
        with patch("semantic_entry.modules.warehouse.postgres.unit_of_work.create_connection") as connect:
            with self.assertRaises(PermissionDeniedError):
                self.executor.query("warehouse.get_stock", 1, {"material_id": 1}, self.actor)
            connect.assert_not_called()
        self.authorizer.is_allowed.assert_called_once_with(self.actor, "warehouse.stock.read")

    def test_prepare_validates_warehouse_input_without_connection(self):
        preparer = CommandPreparer(self.catalog)
        with patch("semantic_entry.modules.warehouse.postgres.unit_of_work.create_connection") as connect:
            missing = preparer.prepare("warehouse.get_stock", 1, {}, self.actor)
            valid = preparer.prepare("warehouse.get_stock", 1, {"material_id": 42}, self.actor)
            self.assertEqual(missing.missing_fields, (("material_id",),))
            self.assertTrue(valid.is_valid)
            self.assertIsInstance(valid.validated_input, GetStockInput)
            self.assertEqual(valid.validated_input.material_id, 42)
            connect.assert_not_called()

    def test_query_schema_exposes_required_positive_integer(self):
        schema = GET_STOCK_SPEC.input_json_schema()
        self.assertEqual(schema["required"], ["material_id"])
        self.assertEqual(schema["properties"]["material_id"]["type"], "integer")
        self.assertEqual(schema["properties"]["material_id"]["exclusiveMinimum"], 0)
        self.assertFalse(schema["additionalProperties"])

    def test_query_contract_import_does_not_load_postgres_or_configuration(self):
        code = '''
import builtins
original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in {'database', 'config', 'app', 'psycopg2', 'flask'}:
        raise AssertionError('Unexpected dependency: ' + name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
from semantic_entry.modules.warehouse.queries import GET_STOCK_SPEC
assert GET_STOCK_SPEC.validate_input({'material_id': 42}).material_id == 42
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()
