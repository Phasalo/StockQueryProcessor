import unittest
from datetime import date
from decimal import Decimal
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
from semantic_entry.modules.warehouse.commands.receipts import (
    RECORD_RECEIPT_SPEC,
    ReceiptPreview,
    RecordReceiptInput,
    RecordReceiptResult,
)
from semantic_entry.modules.warehouse.postgres.handlers.receipt_preview import (
    ReceiptPreviewHandler,
)


class RecordReceiptCommandTest(unittest.TestCase):
    def setUp(self):
        registry = CommandRegistry()
        registry.register(RECORD_RECEIPT_SPEC)
        self.authorizer = Mock()
        self.authorizer.is_allowed.return_value = True
        self.catalog = CommandCatalog(registry, self.authorizer)
        self.preparer = CommandPreparer(self.catalog)
        self.actor = Actor("receiver")
        self.payload = {
            "material_id": 1,
            "supplier_id": 2,
            "actual_date": "2026-11-10",
            "quantity_kg": "12.50",
        }

    def prepare(self, payload):
        return self.preparer.prepare("warehouse.record_receipt", 1, payload, self.actor)

    def test_prepare_returns_typed_unplanned_receipt(self):
        result = self.prepare(self.payload)
        self.assertTrue(result.is_valid)
        self.assertIsInstance(result.validated_input, RecordReceiptInput)
        self.assertEqual(result.validated_input.quantity_kg, Decimal("12.50"))
        self.assertEqual(result.validated_input.actual_date, date(2026, 11, 10))
        self.assertIsNone(result.validated_input.planned_receipt_id)
        self.assertEqual(result.actor_id, "receiver")
        self.authorizer.is_allowed.assert_called_once_with(self.actor, "warehouse.receipt.write")

    def test_receipt_can_reference_one_plan(self):
        result = self.prepare({**self.payload, "planned_receipt_id": 7})
        self.assertTrue(result.is_valid)
        self.assertEqual(result.validated_input.planned_receipt_id, 7)

    def test_missing_fields_request_material_supplier_date_and_quantity(self):
        result = self.prepare({})
        self.assertFalse(result.is_valid)
        self.assertEqual(result.missing_fields, (
            ("material_id",), ("supplier_id",), ("actual_date",), ("quantity_kg",),
        ))

    def test_identifiers_require_positive_integers_without_coercion(self):
        for field in ("material_id", "supplier_id", "planned_receipt_id"):
            for value in (True, "1", 0, -1):
                with self.subTest(field=field, value=value):
                    result = self.prepare({**self.payload, field: value})
                    self.assertFalse(result.is_valid)
                    self.assertIn((field,), [error.path for error in result.errors])

    def test_quantity_rejects_invalid_values_without_rounding(self):
        for value in ("0", "-0.01", "1.001", "100000000", "NaN", "Infinity", "-Infinity", True):
            with self.subTest(value=value):
                result = self.prepare({**self.payload, "quantity_kg": value})
                self.assertFalse(result.is_valid)
                self.assertIsNone(result.validated_input)
                self.assertEqual([error.path for error in result.errors], [("quantity_kg",)])

    def test_valid_quantity_limits_and_trailing_zeros_preserve_decimal(self):
        for value in ("0.01", "99999999.99", "1.2300"):
            with self.subTest(value=value):
                result = self.prepare({**self.payload, "quantity_kg": value})
                self.assertTrue(result.is_valid)
                self.assertEqual(result.validated_input.quantity_kg, Decimal(value))
                self.assertIsInstance(result.validated_input.quantity_kg, Decimal)

    def test_invalid_date_and_unknown_fields_are_reported(self):
        for value in (None, "", "2026-02-30"):
            with self.subTest(value=value):
                result = self.prepare({**self.payload, "actual_date": value})
                self.assertFalse(result.is_valid)
                self.assertEqual([error.path for error in result.errors], [("actual_date",)])
        result = self.prepare({**self.payload, "confirmed": True})
        self.assertEqual([(error.path, error.code) for error in result.errors], [(("confirmed",), "extra_forbidden")])

    def test_prepare_denies_access_before_parameter_validation(self):
        self.authorizer.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.prepare({})

    def test_read_executor_rejects_receipt_command(self):
        executor = QueryExecutor(self.catalog)
        with self.assertRaisesRegex(ValueError, "READ"):
            executor.query("warehouse.record_receipt", 1, self.payload, self.actor)

    def test_invalid_input_does_not_open_preview_connection(self):
        self.preparer.register_preview(RECORD_RECEIPT_SPEC, ReceiptPreview, ReceiptPreviewHandler(WarehouseConfig()))
        with patch("semantic_entry.modules.warehouse.postgres.unit_of_work.create_connection") as connect:
            result = self.prepare({})
            self.assertFalse(result.is_valid)
            self.assertEqual(result.missing_fields, (("material_id",), ("supplier_id",), ("actual_date",), ("quantity_kg",)))
            connect.assert_not_called()

    def test_denied_access_does_not_open_preview_connection(self):
        self.preparer.register_preview(RECORD_RECEIPT_SPEC, ReceiptPreview, ReceiptPreviewHandler(WarehouseConfig()))
        self.authorizer.is_allowed.return_value = False
        with patch("semantic_entry.modules.warehouse.postgres.unit_of_work.create_connection") as connect:
            with self.assertRaises(PermissionDeniedError):
                self.prepare(self.payload)
            connect.assert_not_called()

    def test_result_distinguishes_transaction_and_receipt_identifiers(self):
        result = RECORD_RECEIPT_SPEC.validate_result({"transaction_id": 10, "receipt_id": 3})
        self.assertIsInstance(result, RecordReceiptResult)
        self.assertEqual((result.transaction_id, result.receipt_id), (10, 3))
        for payload in (
            {"id": 3},
            {"transaction_id": 10},
            {"transaction_id": 10, "receipt_id": True},
            {"transaction_id": 0, "receipt_id": 3},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    RECORD_RECEIPT_SPEC.validate_result(payload)


if __name__ == "__main__":
    unittest.main()
