import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from os import environ, getenv
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from config import WarehouseConfig
from semantic_entry.core import Actor, CommandCatalog, CommandPreparer, CommandRegistry
from semantic_entry.core.draft_manager import DraftManager
from semantic_entry.core.drafts import DraftState
from semantic_entry.core.memory_drafts import MemoryDraftStore
from semantic_entry.modules.warehouse.commands.receipts import (
    RECORD_RECEIPT_SPEC,
    ReceiptPreview,
    RecordReceiptInput,
)
from semantic_entry.modules.warehouse.enums import TransactionType
from semantic_entry.modules.warehouse.postgres.catalog import (
    CustomerRepository,
    FilmTypeRepository,
    MaterialRepository,
    SupplierRepository,
)
from semantic_entry.modules.warehouse.postgres.connection import create_connection
from semantic_entry.modules.warehouse.postgres.movements import (
    IssueRecord,
    MovementRepository,
    ReceiptRecord,
)
from semantic_entry.modules.warehouse.postgres.plans import PlanRepository
from semantic_entry.modules.warehouse.postgres.receipt_preview import (
    ReceiptPreviewer,
    ReceiptPreviewHandler,
)
from semantic_entry.modules.warehouse.postgres.transactions import TransactionRepository
from semantic_entry.modules.warehouse.postgres.unit_of_work import UnitOfWork
from semantic_entry.modules.warehouse.service import Warehouse


class MovementRepositoryPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = getenv("TEST_DATABASE_DSN")
        if not dsn:
            raise unittest.SkipTest("Set TEST_DATABASE_DSN to run PostgreSQL tests")
        settings = parse_dsn(dsn)
        cls.admin_config = WarehouseConfig(
            database=settings["dbname"], user=settings["user"],
            password=settings.get("password", ""), host=settings["host"],
            port=int(settings.get("port", 5432)),
        )
        cls.config = replace(cls.admin_config, database="movements_test_" + uuid4().hex)
        with closing(create_connection(cls.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.config.database)))
        cls.addClassCleanup(cls.drop_database)
        env = environ.copy()
        env.update(
            PGDATABASE=cls.config.database, PGUSER=cls.config.user,
            PGPASSWORD=cls.config.password, PGHOST=cls.config.host, PGPORT=str(cls.config.port),
        )
        result = subprocess.run(
            [
                sys.executable, "-m", "pgmigrate", "-d", str(Path(__file__).resolve().parent.parent),
                "-t", "latest", "--check_serial_versions", "migrate",
            ],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        with UnitOfWork(cls.config) as uow:
            film_type = FilmTypeRepository(uow.connection).find_by_code("BOPP")
            materials = MaterialRepository(uow.connection)
            cls.material_id = materials.add(film_type.id, "Brand", 1200, 25)
            cls.other_material_id = materials.add(film_type.id, "Other", 1200, 25)
            suppliers = SupplierRepository(uow.connection)
            cls.supplier_id = suppliers.add("Supplier")
            cls.other_supplier_id = suppliers.add("Other supplier")
            customers = CustomerRepository(uow.connection)
            cls.customer_id = customers.add("Customer", "001")
            cls.other_customer_id = customers.add("Other customer", "002")

    @classmethod
    def drop_database(cls):
        with closing(create_connection(cls.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.config.database)))

    def setUp(self):
        self.actual_date = date(2026, 11, 10)
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("TRUNCATE transactions RESTART IDENTITY CASCADE")

    def create_plan(self, kind):
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            if kind == "receipt":
                return plans.add_receipt(
                    self.material_id, self.supplier_id, date(2026, 11, 1),
                    self.actual_date, Decimal("100"),
                )
            return plans.add_consumption(self.material_id, self.customer_id, Decimal("-100"))

    def receipt_parameters(self, **changes):
        return RecordReceiptInput(**{
            "material_id": self.material_id,
            "supplier_id": self.supplier_id,
            "actual_date": self.actual_date,
            "quantity_kg": Decimal("12.50"),
            **changes,
        })

    def receipt_preview(self, **changes):
        with UnitOfWork(self.config) as uow:
            return ReceiptPreviewer(uow.connection).preview(self.receipt_parameters(**changes))

    def receipt_preparer(self):
        registry = CommandRegistry()
        registry.register(RECORD_RECEIPT_SPEC)
        authorizer = Mock()
        authorizer.is_allowed.return_value = True
        preparer = CommandPreparer(CommandCatalog(registry, authorizer))
        preparer.register_preview(RECORD_RECEIPT_SPEC, ReceiptPreview, ReceiptPreviewHandler(self.config))
        return preparer

    def receipt_preparation(self, **changes):
        payload = self.receipt_parameters(**changes).model_dump(mode="json")
        return self.receipt_preparer().prepare("warehouse.record_receipt", 1, payload, Actor("receiver"))

    def test_receipt_draft_revision_rechecks_plan_and_replaces_previous_preview(self):
        plan = self.create_plan("receipt")
        actor = Actor("receiver")
        preparer = self.receipt_preparer()
        payload = self.receipt_parameters(planned_receipt_id=plan.id).model_dump(mode="json")
        prepared = preparer.prepare("warehouse.record_receipt", 1, payload, actor)
        store = MemoryDraftStore()
        draft = store.create(payload, prepared, lifetime=timedelta(minutes=5))
        manager = DraftManager(preparer, store)
        revised = manager.revise(draft.draft_id, 1, {"quantity_kg": "150.00"}, actor)
        self.assertEqual((revised.version, revised.state), (2, DraftState.READY))
        self.assertEqual(revised.preparation.preview.parameters.quantity_kg, Decimal("150.00"))
        with UnitOfWork(self.config) as uow:
            PlanRepository(uow.connection).cancel(plan.transaction_id, Decimal("100"), "Closed")
        rechecked = manager.revise(draft.draft_id, 2, {}, actor)
        self.assertEqual((rechecked.version, rechecked.state), (3, DraftState.NEEDS_INPUT))
        self.assertIsNone(rechecked.preparation.preview)
        self.assertEqual([(error.path, error.code) for error in rechecked.preparation.errors], [(("planned_receipt_id",), "plan_closed")])
        self.assertEqual(store.get(draft.draft_id, actor), rechecked)
        self.assertEqual(self.count_rows("receipts"), 0)
        self.assertEqual(self.count_rows("transactions"), 1)

    def test_receipt_preparation_builds_preview_through_core(self):
        result = self.receipt_preparation()
        self.assertTrue(result.is_valid)
        self.assertIsInstance(result.preview, ReceiptPreview)
        self.assertEqual(result.actor_id, "receiver")
        self.assertEqual(result.preview.parameters.quantity_kg, Decimal("12.50"))
        self.assertEqual(result.preview.supplier_name, "Supplier")
        self.assertEqual(self.count_rows("transactions"), 0)
        self.assertEqual(self.count_rows("receipts"), 0)

    def test_receipt_preparation_returns_domain_errors_through_core(self):
        result = self.receipt_preparation(material_id=999999)
        self.assertFalse(result.is_valid)
        self.assertIsNone(result.preview)
        self.assertEqual(result.validated_input.material_id, 999999)
        self.assertEqual([(error.path, error.code) for error in result.errors], [(("material_id",), "not_found")])
        self.assertEqual(self.count_rows("transactions"), 0)

    def test_receipt_preview_returns_card_without_creating_movement(self):
        result = self.receipt_preview()
        self.assertTrue(result.is_valid)
        self.assertIsInstance(result.preview, ReceiptPreview)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.preview.parameters.material_id, self.material_id)
        self.assertEqual(result.preview.parameters.quantity_kg, Decimal("12.50"))
        self.assertEqual(result.preview.parameters.actual_date, self.actual_date)
        self.assertEqual(result.preview.supplier_name, "Supplier")
        self.assertEqual((result.preview.film_type_code, result.preview.brand), ("BOPP", "Brand"))
        self.assertEqual((result.preview.format, result.preview.thickness), (1200, 25))
        self.assertIsNone(result.preview.plan_remaining_quantity_kg)
        self.assertEqual(result.model_dump(mode="json")["preview"]["parameters"]["quantity_kg"], "12.50")
        self.assertEqual(self.count_rows("transactions"), 0)
        self.assertEqual(self.count_rows("receipts"), 0)

    def test_receipt_preview_reports_missing_material_and_supplier(self):
        result = self.receipt_preview(material_id=999999, supplier_id=999999)
        self.assertFalse(result.is_valid)
        self.assertIsNone(result.preview)
        self.assertEqual([(error.path, error.code) for error in result.errors], [
            (("material_id",), "not_found"), (("supplier_id",), "not_found"),
        ])

    def test_receipt_preview_reports_missing_plan(self):
        result = self.receipt_preview(planned_receipt_id=999999)
        self.assertFalse(result.is_valid)
        self.assertEqual([(error.path, error.code) for error in result.errors], [(("planned_receipt_id",), "not_found")])

    def test_receipt_preview_reports_plan_material_and_supplier_mismatches(self):
        plan = self.create_plan("receipt")
        result = self.receipt_preview(
            planned_receipt_id=plan.id, material_id=self.other_material_id,
            supplier_id=self.other_supplier_id,
        )
        self.assertFalse(result.is_valid)
        self.assertEqual([(error.path, error.code) for error in result.errors], [
            (("material_id",), "plan_material_mismatch"), (("supplier_id",), "plan_supplier_mismatch"),
        ])

    def test_receipt_preview_accepts_any_positive_quantity_for_open_plan(self):
        plan = self.create_plan("receipt")
        with UnitOfWork(self.config) as uow:
            PlanRepository(uow.connection).cancel(plan.transaction_id, Decimal("25"), "Partial")
        for quantity in (Decimal("10"), Decimal("150")):
            with self.subTest(quantity=quantity):
                result = self.receipt_preview(planned_receipt_id=plan.id, quantity_kg=quantity)
                self.assertTrue(result.is_valid)
                self.assertEqual(result.preview.plan_remaining_quantity_kg, Decimal("75"))
                self.assertEqual(result.preview.parameters.quantity_kg, quantity)
        with UnitOfWork(self.config) as uow:
            unchanged = PlanRepository(uow.connection).get_receipt(plan.id)
        self.assertFalse(unchanged.is_closed)
        self.assertEqual(unchanged.remaining_quantity_kg, Decimal("75"))
        self.assertEqual(self.count_rows("transactions"), 1)
        self.assertEqual(self.count_rows("receipts"), 0)

    def test_receipt_preview_rejects_cancelled_and_executed_plans(self):
        for state in ("cancelled", "executed"):
            with self.subTest(state=state):
                plan = self.create_plan("receipt")
                with UnitOfWork(self.config) as uow:
                    if state == "cancelled":
                        PlanRepository(uow.connection).cancel(plan.transaction_id, Decimal("100"), "All")
                    else:
                        MovementRepository(uow.connection).add_receipt(
                            self.material_id, self.supplier_id, self.actual_date,
                            Decimal("50"), planned_receipt_id=plan.id,
                        )
                before = self.count_rows("transactions")
                result = self.receipt_preview(planned_receipt_id=plan.id)
                self.assertFalse(result.is_valid)
                self.assertEqual([(error.path, error.code) for error in result.errors], [(("planned_receipt_id",), "plan_closed")])
                self.assertEqual(self.count_rows("transactions"), before)

    def test_receipt_preview_uses_callers_uncommitted_catalog_data(self):
        with UnitOfWork(self.config) as uow:
            film_type = FilmTypeRepository(uow.connection).find_by_code("BOPP")
            material_id = MaterialRepository(uow.connection).add(film_type.id, "Uncommitted", 1000, 20)
            supplier_id = SupplierRepository(uow.connection).add("Uncommitted")
            result = ReceiptPreviewer(uow.connection).preview(self.receipt_parameters(
                material_id=material_id, supplier_id=supplier_id,
            ))
            self.assertTrue(result.is_valid)
            self.assertEqual((result.preview.brand, result.preview.supplier_name), ("Uncommitted", "Uncommitted"))
            uow.rollback()
        with UnitOfWork(self.config) as uow:
            self.assertIsNone(MaterialRepository(uow.connection).get(material_id))
            self.assertIsNone(SupplierRepository(uow.connection).get(supplier_id))

    def add_movement(self, connection, kind, plan_id=None, quantity=Decimal("50")):
        movements = MovementRepository(connection)
        if kind == "receipt":
            return movements.add_receipt(
                self.material_id, self.supplier_id, self.actual_date, quantity,
                planned_receipt_id=plan_id,
            )
        return movements.add_issue(
            self.material_id, self.customer_id, self.actual_date, quantity.copy_negate(),
            planned_consumption_id=plan_id,
        )

    def count_rows(self, table):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
                return cursor.fetchone()[0]

    def saved_movement(self, kind, transaction_id):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                table = "receipts" if kind == "receipt" else "issues"
                cursor.execute(
                    sql.SQL("SELECT id FROM {} WHERE transaction_id = %s").format(sql.Identifier(table)),
                    (transaction_id,),
                )
                movement_id = cursor.fetchone()[0]
            movements = MovementRepository(uow.connection)
            return movements.get_receipt(movement_id) if kind == "receipt" else movements.get_issue(movement_id)

    def test_facade_adjustments_store_signed_deltas_and_allow_negative_balance(self):
        warehouse = Warehouse(self.config)
        positive_id = warehouse.create_adjustment(self.material_id, Decimal("10"))
        negative_id = warehouse.create_adjustment(self.material_id, Decimal("-25"))
        self.assertNotEqual(positive_id, negative_id)
        with UnitOfWork(self.config) as uow:
            journal = TransactionRepository(uow.connection)
            positive = journal.get(positive_id)
            negative = journal.get(negative_id)
            self.assertEqual(positive.transaction_type, "ADJUSTMENT")
            self.assertEqual(negative.transaction_type, "ADJUSTMENT")
            self.assertEqual(positive.material_id, self.material_id)
            self.assertEqual(negative.material_id, self.material_id)
            self.assertEqual(positive.quantity_delta_kg, Decimal("10"))
            self.assertEqual(negative.quantity_delta_kg, Decimal("-25"))
            with uow.connection.cursor() as cursor:
                cursor.execute("SELECT sum(quantity_delta_kg) FROM transactions")
                self.assertEqual(cursor.fetchone()[0], Decimal("-15"))
        self.assertEqual(self.count_rows("receipts"), 0)
        self.assertEqual(self.count_rows("issues"), 0)

    def test_facade_adjustment_does_not_change_plans_or_cancellations(self):
        receipt_plan = self.create_plan("receipt")
        issue_plan = self.create_plan("issue")
        warehouse = Warehouse(self.config)
        warehouse.cancel_plan(receipt_plan.transaction_id, Decimal("10"), "Partial")
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            expected_receipt = plans.get_receipt(receipt_plan.id)
            expected_issue = plans.get_consumption(issue_plan.id)
        warehouse.create_adjustment(self.material_id, Decimal("5"))
        warehouse.create_adjustment(self.material_id, Decimal("-7"))
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            self.assertEqual(plans.get_receipt(receipt_plan.id), expected_receipt)
            self.assertEqual(plans.get_consumption(issue_plan.id), expected_issue)
        self.assertEqual(self.count_rows("plan_cancellations"), 1)
        self.assertEqual(self.count_rows("transactions"), 4)

    def test_facade_adjustment_rejects_invalid_quantities_without_rounding(self):
        cases = (
            (Decimal("0"), ValueError), (Decimal("NaN"), ValueError),
            (Decimal("Infinity"), ValueError), (Decimal("100000000"), ValueError),
            (Decimal("-100000000"), ValueError), (Decimal("1.001"), ValueError),
            (1.1, TypeError), ("10", TypeError),
        )
        for quantity, error in cases:
            with self.subTest(quantity=quantity):
                with self.assertRaises(error):
                    Warehouse(self.config).create_adjustment(self.material_id, quantity)
        self.assertEqual(self.count_rows("transactions"), 0)

    def test_facade_adjustment_requires_existing_material(self):
        with self.assertRaisesRegex(ValueError, "не найден"):
            Warehouse(self.config).create_adjustment(999999, Decimal("10"))
        self.assertEqual(self.count_rows("transactions"), 0)

    def test_facade_adjustment_obeys_material_lock(self):
        def open_with_timeout(config):
            connection = create_connection(config)
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '100ms'")
            return connection

        with UnitOfWork(self.config) as holder:
            with holder.connection.cursor() as cursor:
                cursor.execute("SELECT id FROM materials WHERE id = %s FOR UPDATE", (self.material_id,))
            with patch("semantic_entry.modules.warehouse.postgres.unit_of_work.create_connection", side_effect=open_with_timeout):
                with self.assertRaises(psycopg2.errors.LockNotAvailable):
                    Warehouse(self.config).create_adjustment(self.material_id, Decimal("10"))
        self.assertEqual(self.count_rows("transactions"), 0)

    def test_facade_receipt_preserves_positional_arguments_and_uses_plan_supplier(self):
        with UnitOfWork(self.config) as uow:
            TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("1"))
        plan = self.create_plan("receipt")
        warehouse = Warehouse(self.config)
        warehouse.cancel_plan(plan.transaction_id, Decimal("30"), "Partial")
        transaction_id = warehouse.create_receipt(
            self.material_id, plan.id, self.actual_date, Decimal("150"),
        )
        receipt = self.saved_movement("receipt", transaction_id)
        self.assertNotEqual(receipt.id, transaction_id)
        self.assertEqual(receipt.transaction_id, transaction_id)
        self.assertEqual(receipt.material_id, self.material_id)
        self.assertEqual(receipt.supplier_id, self.supplier_id)
        self.assertEqual(receipt.actual_date, self.actual_date)
        self.assertEqual(receipt.planned_receipt_id, plan.id)
        self.assertEqual(receipt.quantity_delta_kg, Decimal("150"))
        current = warehouse.get_planned_receipt(plan.id)
        self.assertTrue(current["is_closed"])
        self.assertEqual(current["delta_kg"], Decimal("100"))
        self.assertEqual(current["cancelled_quantity_kg"], Decimal("30"))
        self.assertEqual(current["remaining_quantity_kg"], Decimal("0"))

    def test_facade_unplanned_receipt_requires_explicit_supplier(self):
        warehouse = Warehouse(self.config)
        with self.assertRaisesRegex(ValueError, "supplier_id"):
            warehouse.create_receipt(self.material_id, None, self.actual_date, Decimal("10"))
        self.assertEqual(self.count_rows("transactions"), 0)
        transaction_id = warehouse.create_receipt(
            self.material_id, None, self.actual_date, Decimal("10"),
            supplier_id=self.supplier_id,
        )
        receipt = self.saved_movement("receipt", transaction_id)
        self.assertEqual(receipt.supplier_id, self.supplier_id)
        self.assertIsNone(receipt.planned_receipt_id)
        self.assertEqual(self.count_rows("planned_receipts"), 0)

    def test_facade_planned_receipt_checks_explicit_supplier(self):
        plan = self.create_plan("receipt")
        warehouse = Warehouse(self.config)
        with self.assertRaisesRegex(ValueError, "Контрагент"):
            warehouse.create_receipt(
                self.material_id, plan.id, self.actual_date, Decimal("10"),
                supplier_id=self.other_supplier_id,
            )
        self.assertEqual(self.count_rows("transactions"), 1)
        self.assertFalse(warehouse.get_planned_receipt(plan.id)["is_closed"])
        transaction_id = warehouse.create_receipt(
            self.material_id, plan.id, self.actual_date, Decimal("10"),
            supplier_id=self.supplier_id,
        )
        self.assertEqual(self.saved_movement("receipt", transaction_id).planned_receipt_id, plan.id)

    def test_facade_invalid_receipt_plan_does_not_create_fact(self):
        plan = self.create_plan("receipt")
        warehouse = Warehouse(self.config)
        for material_id, plan_id, supplier in (
            (self.material_id, 999999, None),
            (self.material_id, 999999, self.supplier_id),
            (self.other_material_id, plan.id, None),
        ):
            with self.subTest(material_id=material_id, plan_id=plan_id, supplier=supplier):
                with self.assertRaises(ValueError):
                    warehouse.create_receipt(
                        material_id, plan_id, self.actual_date, Decimal("10"), supplier_id=supplier,
                    )
        self.assertEqual(self.count_rows("transactions"), 1)
        self.assertEqual(self.count_rows("receipts"), 0)

    def test_facade_missing_receipt_date_rolls_back_journal(self):
        with self.assertRaises(psycopg2.errors.NotNullViolation):
            Warehouse(self.config).create_receipt(
                self.material_id, None, None, Decimal("10"), supplier_id=self.supplier_id,
            )
        self.assertEqual(self.count_rows("transactions"), 0)
        self.assertEqual(self.count_rows("receipts"), 0)

    def test_facade_issue_allows_negative_stock_and_explicitly_absent_date(self):
        transaction_id = Warehouse(self.config).create_issue(
            self.material_id, self.customer_id, None, Decimal("-150"),
        )
        issue = self.saved_movement("issue", transaction_id)
        self.assertEqual(issue.customer_id, self.customer_id)
        self.assertEqual(issue.material_id, self.material_id)
        self.assertEqual(issue.quantity_delta_kg, Decimal("-150"))
        self.assertIsNone(issue.actual_date)
        self.assertIsNone(issue.planned_consumption_id)
        self.assertEqual(self.count_rows("consumption"), 0)
        self.assertEqual(self.count_rows("receipts"), 0)
        with UnitOfWork(self.config) as uow:
            transaction = TransactionRepository(uow.connection).get(transaction_id)
            self.assertEqual(transaction.transaction_type, "ISSUE")
            self.assertEqual(transaction.quantity_delta_kg, Decimal("-150"))

    def test_facade_issue_closes_plan_and_rejects_repeated_execution(self):
        with UnitOfWork(self.config) as uow:
            TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("1"))
        plan = self.create_plan("issue")
        warehouse = Warehouse(self.config)
        warehouse.cancel_plan(plan.transaction_id, Decimal("30"), "Partial")
        transaction_id = warehouse.create_issue(
            self.material_id, self.customer_id, self.actual_date, Decimal("-150"),
            planned_consumption_id=plan.id,
        )
        issue = self.saved_movement("issue", transaction_id)
        self.assertNotEqual(issue.id, transaction_id)
        self.assertEqual(issue.planned_consumption_id, plan.id)
        self.assertEqual(issue.quantity_delta_kg, Decimal("-150"))
        self.assertEqual(issue.actual_date, self.actual_date)
        with UnitOfWork(self.config) as uow:
            current = PlanRepository(uow.connection).get_by_transaction_id(plan.transaction_id)
            self.assertTrue(current.is_executed)
            self.assertEqual(current.quantity_delta_kg, Decimal("-100"))
            self.assertEqual(current.remaining_quantity_kg, Decimal("0"))
        with self.assertRaisesRegex(ValueError, "закрыт"):
            warehouse.create_issue(
                self.material_id, self.customer_id, self.actual_date, Decimal("-1"),
                planned_consumption_id=plan.id,
            )
        self.assertEqual(self.count_rows("issues"), 1)
        self.assertEqual(self.count_rows("transactions"), 3)

    def test_facade_issue_requires_matching_customer_and_negative_delta(self):
        plan = self.create_plan("issue")
        warehouse = Warehouse(self.config)
        for customer_id, quantity in (
            (self.other_customer_id, Decimal("-10")),
            (self.customer_id, Decimal("10")),
        ):
            with self.subTest(customer_id=customer_id, quantity=quantity):
                with self.assertRaises(ValueError):
                    warehouse.create_issue(
                        self.material_id, customer_id, self.actual_date, quantity,
                        planned_consumption_id=plan.id,
                    )
        self.assertEqual(self.count_rows("issues"), 0)
        self.assertEqual(self.count_rows("transactions"), 1)

    def test_unplanned_movements_preserve_ids_parties_dates_and_signs(self):
        with UnitOfWork(self.config) as uow:
            journal = TransactionRepository(uow.connection)
            journal.add("ADJUSTMENT", self.material_id, Decimal("1"))
            movements = MovementRepository(uow.connection)
            receipt = self.add_movement(uow.connection, "receipt")
            issue = self.add_movement(uow.connection, "issue")
            self.assertIsInstance(receipt, ReceiptRecord)
            self.assertIsInstance(issue, IssueRecord)
            self.assertNotEqual(receipt.id, receipt.transaction_id)
            self.assertNotEqual(issue.id, issue.transaction_id)
            self.assertEqual(receipt.supplier_id, self.supplier_id)
            self.assertEqual(issue.customer_id, self.customer_id)
            self.assertEqual(receipt.quantity_delta_kg, Decimal("50"))
            self.assertEqual(issue.quantity_delta_kg, Decimal("-50"))
            self.assertEqual(receipt.actual_date, self.actual_date)
            self.assertEqual(issue.actual_date, self.actual_date)
            self.assertIsNone(receipt.planned_receipt_id)
            self.assertIsNone(issue.planned_consumption_id)
            self.assertEqual(journal.get(receipt.transaction_id).transaction_type, "RECEIPT")
            self.assertEqual(journal.get(issue.transaction_id).transaction_type, TransactionType.ISSUE.name)
            self.assertEqual(movements.get_receipt(receipt.id), receipt)
            self.assertEqual(movements.get_issue(issue.id), issue)
        with UnitOfWork(self.config) as uow:
            movements = MovementRepository(uow.connection)
            self.assertEqual(movements.get_receipt(receipt.id), receipt)
            self.assertEqual(movements.get_issue(issue.id), issue)
            self.assertIsNone(movements.get_receipt(999999))
            self.assertIsNone(movements.get_issue(999999))

    def test_issue_date_can_be_explicitly_absent(self):
        with UnitOfWork(self.config) as uow:
            movements = MovementRepository(uow.connection)
            issue = movements.add_issue(self.material_id, self.customer_id, None, Decimal("-1"))
            self.assertIsNone(issue.actual_date)
            self.assertIsNone(movements.get_issue(issue.id).actual_date)

    def test_receipt_requires_actual_date_and_rolls_back_journal(self):
        with self.assertRaises(psycopg2.errors.NotNullViolation):
            with UnitOfWork(self.config) as uow:
                MovementRepository(uow.connection).add_receipt(
                    self.material_id, self.supplier_id, None, Decimal("1"),
                )
        self.assertEqual(self.count_rows("transactions"), 0)

    def test_repository_does_not_commit_implicitly(self):
        with UnitOfWork(self.config) as uow:
            self.add_movement(uow.connection, "receipt")
            self.add_movement(uow.connection, "issue")
            self.assertEqual(self.count_rows("transactions"), 0)
            uow.rollback()
        self.assertEqual(self.count_rows("transactions"), 0)
        self.assertEqual(self.count_rows("receipts"), 0)
        self.assertEqual(self.count_rows("issues"), 0)

    def test_detail_error_rolls_back_all_movements_in_context(self):
        for kind in ("receipt", "issue"):
            with self.subTest(kind=kind):
                with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
                    with UnitOfWork(self.config) as uow:
                        self.add_movement(uow.connection, "receipt")
                        movements = MovementRepository(uow.connection)
                        if kind == "receipt":
                            movements.add_receipt(self.material_id, 999999, self.actual_date, Decimal("1"))
                        else:
                            movements.add_issue(self.material_id, 999999, self.actual_date, Decimal("-1"))
                self.assertEqual(self.count_rows("transactions"), 0)
                self.assertEqual(self.count_rows("receipts"), 0)

    def test_missing_material_does_not_create_journal(self):
        with self.assertRaisesRegex(ValueError, "не найден"):
            with UnitOfWork(self.config) as uow:
                MovementRepository(uow.connection).add_receipt(
                    999999, self.supplier_id, self.actual_date, Decimal("1"),
                )
        self.assertEqual(self.count_rows("transactions"), 0)

    def test_invalid_quantities_do_not_create_movements(self):
        for kind, quantity, error in (
            ("receipt", Decimal("-1"), ValueError), ("issue", Decimal("-1"), ValueError),
            ("receipt", Decimal("0"), ValueError), ("issue", Decimal("NaN"), ValueError),
            ("receipt", Decimal("1.001"), ValueError), ("issue", Decimal("100000000"), ValueError),
            ("receipt", 1.1, TypeError),
        ):
            with self.subTest(kind=kind, quantity=quantity):
                with self.assertRaises(error):
                    with UnitOfWork(self.config) as uow:
                        self.add_movement(uow.connection, kind, quantity=quantity)
        self.assertEqual(self.count_rows("transactions"), 0)

    def test_any_fact_quantity_closes_plan_after_partial_cancellation(self):
        for kind in ("receipt", "issue"):
            for quantity in (Decimal("1"), Decimal("70"), Decimal("150")):
                with self.subTest(kind=kind, quantity=quantity):
                    plan = self.create_plan(kind)
                    with UnitOfWork(self.config) as uow:
                        plans = PlanRepository(uow.connection)
                        plans.cancel(plan.transaction_id, Decimal("30"), "Partial")
                        fact = self.add_movement(uow.connection, kind, plan.id, quantity)
                        current = plans.get_by_transaction_id(plan.transaction_id)
                        self.assertEqual(current.quantity_delta_kg, plan.quantity_delta_kg)
                        self.assertEqual(current.cancelled_quantity_kg, Decimal("30"))
                        self.assertEqual(current.remaining_quantity_kg, Decimal("0"))
                        self.assertTrue(current.is_executed)
                        self.assertEqual(fact.quantity_delta_kg.copy_abs(), quantity)
                        linked_id = fact.planned_receipt_id if kind == "receipt" else fact.planned_consumption_id
                        self.assertEqual(linked_id, plan.id)
                    with self.assertRaisesRegex(ValueError, "закрыт"):
                        with UnitOfWork(self.config) as uow:
                            plans = PlanRepository(uow.connection)
                            plans.cancel(plan.transaction_id, Decimal("1"), "Late")

    def test_fully_cancelled_plan_cannot_be_executed(self):
        for kind in ("receipt", "issue"):
            with self.subTest(kind=kind):
                plan = self.create_plan(kind)
                with UnitOfWork(self.config) as uow:
                    PlanRepository(uow.connection).cancel(plan.transaction_id, Decimal("100"), "All")
                with self.assertRaisesRegex(ValueError, "закрыт"):
                    with UnitOfWork(self.config) as uow:
                        self.add_movement(uow.connection, kind, plan.id)
        self.assertEqual(self.count_rows("receipts"), 0)
        self.assertEqual(self.count_rows("issues"), 0)
        self.assertEqual(self.count_rows("transactions"), 2)

    def test_plan_cannot_be_executed_twice(self):
        for kind in ("receipt", "issue"):
            with self.subTest(kind=kind):
                plan = self.create_plan(kind)
                with UnitOfWork(self.config) as uow:
                    self.add_movement(uow.connection, kind, plan.id)
                with self.assertRaisesRegex(ValueError, "закрыт"):
                    with UnitOfWork(self.config) as uow:
                        self.add_movement(uow.connection, kind, plan.id)
        self.assertEqual(self.count_rows("transactions"), 4)

    def test_plan_requires_matching_material_and_party(self):
        for kind in ("receipt", "issue"):
            plan = self.create_plan(kind)
            party_id = self.supplier_id if kind == "receipt" else self.customer_id
            other_party = self.other_supplier_id if kind == "receipt" else self.other_customer_id
            for material, party in ((self.other_material_id, party_id), (self.material_id, other_party)):
                with self.subTest(kind=kind, material=material, party=party):
                    with self.assertRaisesRegex(ValueError, "не совпадает"):
                        with UnitOfWork(self.config) as uow:
                            movements = MovementRepository(uow.connection)
                            if kind == "receipt":
                                movements.add_receipt(material, party, self.actual_date, Decimal("1"), planned_receipt_id=plan.id)
                            else:
                                movements.add_issue(material, party, self.actual_date, Decimal("-1"), planned_consumption_id=plan.id)
        self.assertEqual(self.count_rows("transactions"), 2)

    def test_plan_link_uses_detail_id_and_rejects_missing_plan(self):
        for kind in ("receipt", "issue"):
            with UnitOfWork(self.config) as uow:
                TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("1"))
            plan = self.create_plan(kind)
            self.assertNotEqual(plan.id, plan.transaction_id)
            for invalid_id in (999999, plan.transaction_id):
                with self.subTest(kind=kind, invalid_id=invalid_id):
                    with self.assertRaisesRegex(ValueError, "не найден"):
                        with UnitOfWork(self.config) as uow:
                            self.add_movement(uow.connection, kind, invalid_id)

    def test_execution_obeys_material_and_plan_locks(self):
        for kind in ("receipt", "issue"):
            plan = self.create_plan(kind)
            for table, row_id in (("materials", self.material_id), ("transactions", plan.transaction_id)):
                with self.subTest(kind=kind, table=table):
                    with UnitOfWork(self.config) as holder:
                        with holder.connection.cursor() as cursor:
                            cursor.execute(
                                sql.SQL("SELECT id FROM {} WHERE id = %s FOR UPDATE").format(sql.Identifier(table)),
                                (row_id,),
                            )
                        with self.assertRaises(psycopg2.errors.LockNotAvailable):
                            with UnitOfWork(self.config) as contender:
                                with contender.connection.cursor() as cursor:
                                    cursor.execute("SET LOCAL lock_timeout = '100ms'")
                                self.add_movement(contender.connection, kind, plan.id)
        self.assertEqual(self.count_rows("transactions"), 2)

    def test_parallel_execution_creates_only_one_fact(self):
        for kind in ("receipt", "issue"):
            plan = self.create_plan(kind)
            barrier = Barrier(2)

            def execute(kind=kind, plan=plan, barrier=barrier):
                with UnitOfWork(self.config) as uow:
                    with uow.connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '3s'")
                    barrier.wait(timeout=5)
                    try:
                        return self.add_movement(uow.connection, kind, plan.id)
                    except ValueError:
                        return None

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(execute) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
            self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.count_rows("transactions"), 4)
        self.assertEqual(self.count_rows("receipts"), 1)
        self.assertEqual(self.count_rows("issues"), 1)

    def test_full_cancellation_and_execution_are_mutually_exclusive(self):
        for kind in ("receipt", "issue"):
            plan = self.create_plan(kind)
            barrier = Barrier(2)

            def perform(operation, kind=kind, plan=plan, barrier=barrier):
                with UnitOfWork(self.config) as uow:
                    with uow.connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '3s'")
                    barrier.wait(timeout=5)
                    try:
                        if operation == "execute":
                            self.add_movement(uow.connection, kind, plan.id)
                        else:
                            PlanRepository(uow.connection).cancel(
                                plan.transaction_id, Decimal("100"), "Concurrent",
                            )
                    except ValueError:
                        return None
                    return operation

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(perform, operation) for operation in ("execute", "cancel")]
                results = [future.result(timeout=10) for future in futures]
            self.assertEqual(sum(result is not None for result in results), 1)
            with UnitOfWork(self.config) as uow:
                current = PlanRepository(uow.connection).get_by_transaction_id(plan.transaction_id)
                self.assertTrue(current.is_closed)
                self.assertEqual(current.is_executed, "execute" in results)
                expected_cancelled = Decimal("0") if current.is_executed else Decimal("100")
                self.assertEqual(current.cancelled_quantity_kg, expected_cancelled)


if __name__ == "__main__":
    unittest.main()
