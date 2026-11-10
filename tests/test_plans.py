import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from os import environ, getenv
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from config import WarehouseConfig
from semantic_entry.modules.warehouse.postgres.connection import create_connection
from semantic_entry.modules.warehouse.postgres.repositories.catalog import (
    CustomerRepository,
    FilmTypeRepository,
    MaterialRepository,
    SupplierRepository,
)
from semantic_entry.modules.warehouse.postgres.repositories.plans import PlanRepository
from semantic_entry.modules.warehouse.postgres.repositories.transactions import (
    TransactionRepository,
)
from semantic_entry.modules.warehouse.postgres.unit_of_work import UnitOfWork
from semantic_entry.modules.warehouse.service import Warehouse


class PlanRepositoryPostgresTest(unittest.TestCase):
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
        name = "plans_test_" + uuid4().hex
        cls.config = replace(cls.admin_config, database=name)
        with closing(create_connection(cls.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        cls.addClassCleanup(cls.drop_database)
        env = environ.copy()
        env.update(
            PGDATABASE=name, PGUSER=cls.config.user, PGPASSWORD=cls.config.password,
            PGHOST=cls.config.host, PGPORT=str(cls.config.port),
        )
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [
                sys.executable, "-m", "pgmigrate", "-d", str(root),
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
                cursor.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.config.database))
                )

    def setUp(self):
        self.order_date = date(2026, 11, 1)
        self.planned_date = date(2026, 11, 10)
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("TRUNCATE transactions RESTART IDENTITY CASCADE")

    def create_plan(self, kind="receipt", quantity=Decimal("100.00")):
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            if kind == "receipt":
                plan = plans.add_receipt(
                    self.material_id, self.supplier_id, self.order_date,
                    self.planned_date, quantity,
                )
            else:
                plan = plans.add_consumption(
                    self.material_id, self.customer_id, quantity.copy_negate(),
                )
        return plan

    def count_rows(self, table):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
                return cursor.fetchone()[0]

    def record_fact(self, plan):
        receipt = plan.transaction_type == "PLANNED_RECEIPT"
        with UnitOfWork(self.config) as uow:
            transaction = TransactionRepository(uow.connection).add(
                "RECEIPT" if receipt else "ISSUE", self.material_id,
                Decimal("50.00") if receipt else Decimal("-50.00"),
            )
            with uow.connection.cursor() as cursor:
                if receipt:
                    cursor.execute(
                        "INSERT INTO receipts "
                        "(transaction_id, supplier_id, planned_receipt_id, actual_date) "
                        "VALUES (%s, %s, %s, %s)",
                        (transaction.id, self.supplier_id, plan.id, self.planned_date),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO issues (transaction_id, customer_id, planned_consumption_id) "
                        "VALUES (%s, %s, %s)",
                        (transaction.id, self.customer_id, plan.id),
                    )

    def test_receipt_plan_has_distinct_ids_and_explicit_dates(self):
        with UnitOfWork(self.config) as uow:
            TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("1"))
            plans = PlanRepository(uow.connection)
            plan = plans.add_receipt(
                self.material_id, self.supplier_id, self.order_date,
                self.planned_date, Decimal("100"),
            )
            self.assertNotEqual(plan.id, plan.transaction_id)
            self.assertEqual(plan.order_date, self.order_date)
            self.assertEqual(plan.planned_date, self.planned_date)
            self.assertEqual(plan.supplier_id, self.supplier_id)
            self.assertIsNone(plan.customer_id)
            self.assertEqual(plan.remaining_quantity_kg, Decimal("100"))
            self.assertEqual(plans.get_receipt(plan.id), plan)
            self.assertEqual(plans.get_by_transaction_id(plan.transaction_id), plan)
            self.assertIsNone(plans.get_receipt(999999))
            self.assertIsNone(plans.get_by_transaction_id(999999))
        with UnitOfWork(self.config) as uow:
            self.assertEqual(PlanRepository(uow.connection).get_receipt(plan.id), plan)

    def test_consumption_plan_keeps_negative_delta_and_positive_open_quantity(self):
        plan = self.create_plan("consumption")
        self.assertEqual(plan.transaction_type, "CONSUMPTION")
        self.assertEqual(plan.quantity_delta_kg, Decimal("-100"))
        self.assertEqual(plan.remaining_quantity_kg, Decimal("100"))
        self.assertEqual(plan.customer_id, self.customer_id)
        self.assertIsNone(plan.supplier_id)
        self.assertFalse(plan.is_closed)
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            self.assertEqual(plans.get_consumption(plan.id), plan)
            self.assertEqual(plans.get_by_transaction_id(plan.transaction_id), plan)
            self.assertIsNone(plans.get_consumption(999999))

    def test_detail_error_rolls_back_journal(self):
        for kind in ("receipt", "consumption"):
            with self.subTest(kind=kind):
                with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
                    with UnitOfWork(self.config) as uow:
                        plans = PlanRepository(uow.connection)
                        if kind == "receipt":
                            plans.add_receipt(
                                self.material_id, 999999, self.order_date,
                                self.planned_date, Decimal("100"),
                            )
                        else:
                            plans.add_consumption(self.material_id, 999999, Decimal("-100"))
                self.assertEqual(self.count_rows("transactions"), 0)

    def test_creation_does_not_commit_implicitly(self):
        with UnitOfWork(self.config) as uow:
            plan = PlanRepository(uow.connection).add_receipt(
                self.material_id, self.supplier_id, self.order_date,
                self.planned_date, Decimal("100"),
            )
            self.assertEqual(self.count_rows("transactions"), 0)
            uow.rollback()
        with UnitOfWork(self.config) as uow:
            self.assertIsNone(PlanRepository(uow.connection).get_receipt(plan.id))
            self.assertIsNone(TransactionRepository(uow.connection).get(plan.transaction_id))

    def test_wrong_signs_and_excess_precision_do_not_create_plans(self):
        for kind, quantity in (
            ("receipt", Decimal("-1")), ("consumption", Decimal("1")),
            ("receipt", Decimal("1.001")), ("consumption", Decimal("NaN")),
        ):
            with self.subTest(kind=kind, quantity=quantity):
                with self.assertRaises(ValueError):
                    with UnitOfWork(self.config) as uow:
                        plans = PlanRepository(uow.connection)
                        if kind == "receipt":
                            plans.add_receipt(
                                self.material_id, self.supplier_id, self.order_date,
                                self.planned_date, quantity,
                            )
                        else:
                            plans.add_consumption(self.material_id, self.customer_id, quantity)
        self.assertEqual(self.count_rows("transactions"), 0)

    def test_multiple_cancellations_are_aggregated_once(self):
        for kind in ("receipt", "consumption"):
            with self.subTest(kind=kind):
                plan = self.create_plan(kind)
                with UnitOfWork(self.config) as uow:
                    plans = PlanRepository(uow.connection)
                    first = plans.cancel(plan.transaction_id, Decimal("10"), "First")
                    plans.cancel(plan.transaction_id, Decimal("15"), "Second")
                    current = plans.get_by_transaction_id(plan.transaction_id)
                    self.assertEqual(first.plan_transaction_id, plan.transaction_id)
                    self.assertEqual(first.quantity_kg, Decimal("10"))
                    self.assertEqual(first.reason, "First")
                    self.assertIsNotNone(first.created_at.utcoffset())
                    self.assertEqual(current.quantity_delta_kg, plan.quantity_delta_kg)
                    self.assertEqual(current.cancelled_quantity_kg, Decimal("25"))
                    self.assertEqual(current.remaining_quantity_kg, Decimal("75"))
                    opened = (
                        plans.find_open_receipts() if kind == "receipt"
                        else plans.find_open_consumptions()
                    )
                    self.assertEqual(opened, [current])
        self.assertEqual(self.count_rows("transactions"), 2)
        self.assertEqual(self.count_rows("plan_cancellations"), 4)

    def test_full_cancellation_closes_plan(self):
        for kind in ("receipt", "consumption"):
            with self.subTest(kind=kind):
                plan = self.create_plan(kind)
                with UnitOfWork(self.config) as uow:
                    plans = PlanRepository(uow.connection)
                    plans.cancel(plan.transaction_id, Decimal("100"), "Cancelled")
                    current = plans.get_by_transaction_id(plan.transaction_id)
                    self.assertTrue(current.is_closed)
                    self.assertFalse(current.is_executed)
                    self.assertEqual(current.remaining_quantity_kg, Decimal("0"))
                    opened = (
                        plans.find_open_receipts() if kind == "receipt"
                        else plans.find_open_consumptions()
                    )
                    self.assertEqual(opened, [])
                with self.assertRaisesRegex(ValueError, "закрыт"):
                    with UnitOfWork(self.config) as uow:
                        PlanRepository(uow.connection).cancel(plan.transaction_id, Decimal("1"), "Again")

    def test_cancellation_cannot_exceed_remaining_quantity(self):
        plan = self.create_plan()
        warehouse = Warehouse(self.config)
        warehouse.cancel_plan(plan.transaction_id, Decimal("30"), "First")
        with self.assertRaisesRegex(ValueError, "превышает"):
            warehouse.cancel_plan(plan.transaction_id, Decimal("80"), "Too much")
        with UnitOfWork(self.config) as uow:
            current = PlanRepository(uow.connection).get_receipt(plan.id)
            self.assertEqual(current.remaining_quantity_kg, Decimal("70"))
        self.assertEqual(self.count_rows("plan_cancellations"), 1)

    def test_invalid_cancellations_leave_no_rows(self):
        plan = self.create_plan()
        cases = [
            (Decimal("0"), "Reason"), (Decimal("-1"), "Reason"),
            (Decimal("1.001"), "Reason"), (Decimal("NaN"), "Reason"),
            (Decimal("1"), ""), (Decimal("1"), "   "),
        ]
        for quantity, reason in cases:
            with self.subTest(quantity=quantity, reason=reason):
                with self.assertRaises(ValueError):
                    Warehouse(self.config).cancel_plan(plan.transaction_id, quantity, reason)
        self.assertEqual(self.count_rows("plan_cancellations"), 0)

    def test_recorded_fact_closes_entire_plan_and_prevents_cancellation(self):
        for kind in ("receipt", "consumption"):
            with self.subTest(kind=kind):
                plan = self.create_plan(kind)
                self.record_fact(plan)
                with UnitOfWork(self.config) as uow:
                    plans = PlanRepository(uow.connection)
                    current = plans.get_by_transaction_id(plan.transaction_id)
                    self.assertTrue(current.is_executed)
                    self.assertTrue(current.is_closed)
                    self.assertEqual(current.remaining_quantity_kg, Decimal("0"))
                    opened = (
                        plans.find_open_receipts() if kind == "receipt"
                        else plans.find_open_consumptions()
                    )
                    self.assertEqual(opened, [])
                with self.assertRaisesRegex(ValueError, "закрыт"):
                    Warehouse(self.config).cancel_plan(plan.transaction_id, Decimal("1"), "Late")

    def test_nonplan_and_incomplete_plan_cannot_be_cancelled(self):
        with UnitOfWork(self.config) as uow:
            journal = TransactionRepository(uow.connection)
            actual = journal.add("RECEIPT", self.material_id, Decimal("1"))
            incomplete = journal.add("PLANNED_RECEIPT", self.material_id, Decimal("1"))
        for transaction_id in (999999, actual.id, incomplete.id):
            with self.subTest(transaction_id=transaction_id):
                with self.assertRaises(ValueError):
                    Warehouse(self.config).cancel_plan(transaction_id, Decimal("1"), "Invalid")
        self.assertEqual(self.count_rows("plan_cancellations"), 0)

    def test_find_open_filters_and_limits(self):
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            first = plans.add_receipt(
                self.material_id, self.supplier_id, self.order_date,
                self.planned_date, Decimal("100"),
            )
            second = plans.add_receipt(
                self.other_material_id, self.other_supplier_id, self.order_date,
                self.planned_date, Decimal("100"),
            )
            self.assertEqual(plans.find_open_receipts(), [first, second])
            self.assertEqual(plans.find_open_receipts(self.material_id), [first])
            self.assertEqual(plans.find_open_receipts(supplier_id=self.other_supplier_id), [second])
            self.assertEqual(plans.find_open_receipts(self.material_id, self.other_supplier_id), [])
            self.assertEqual(plans.find_open_receipts(limit=1), [first])
            self.assertEqual(plans.find_open_receipts(limit=0), [])
            outgoing = plans.add_consumption(self.material_id, self.customer_id, Decimal("-100"))
            plans.add_consumption(self.other_material_id, self.other_customer_id, Decimal("-100"))
            self.assertEqual(plans.find_open_consumptions(self.material_id, self.customer_id), [outgoing])
            self.assertEqual(plans.find_open_consumptions(self.material_id, self.other_customer_id), [])

    def test_facade_keeps_journal_id_and_requires_order_date(self):
        warehouse = Warehouse(self.config)
        with self.assertRaises(TypeError):
            warehouse.create_planned_receipt(
                self.material_id, self.supplier_id, self.planned_date, Decimal("100"),
            )
        with UnitOfWork(self.config) as uow:
            TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("1"))
        transaction_id = warehouse.create_planned_receipt(
            self.material_id, self.supplier_id, self.planned_date, Decimal("100"),
            order_date=self.order_date,
        )
        with UnitOfWork(self.config) as uow:
            plan = PlanRepository(uow.connection).get_by_transaction_id(transaction_id)
        self.assertNotEqual(plan.id, transaction_id)
        result = warehouse.get_planned_receipt(plan.id)
        self.assertEqual(result["id"], plan.id)
        self.assertEqual(result["transaction_id"], transaction_id)
        self.assertEqual(result["film_id"], self.material_id)
        self.assertEqual(result["order_date"], self.order_date)
        self.assertEqual(result["delta_kg"], Decimal("100"))
        self.assertEqual(
            warehouse.find_open_planned_receipts(self.material_id, self.supplier_id), [result],
        )
        self.assertIsNone(warehouse.get_planned_receipt(999999))
        consumption_id = warehouse.create_consumption(
            self.material_id, self.customer_id, Decimal("-10"),
        )
        with UnitOfWork(self.config) as uow:
            self.assertEqual(
                PlanRepository(uow.connection).get_by_transaction_id(consumption_id).customer_id,
                self.customer_id,
            )

    def test_remaining_quantity_is_independent_of_decimal_context(self):
        plan = self.create_plan(quantity=Decimal("99999999.99"))
        with localcontext() as context:
            context.prec = 2
            warehouse = Warehouse(self.config)
            warehouse.cancel_plan(plan.transaction_id, Decimal("0.01"), "First")
            warehouse.cancel_plan(plan.transaction_id, Decimal("0.02"), "Second")
            result = warehouse.get_planned_receipt(plan.id)
            self.assertEqual(result["remaining_quantity_kg"], Decimal("99999999.96"))

    def test_cancellation_obeys_material_and_plan_locks(self):
        plan = self.create_plan()
        for table, row_id in (("materials", self.material_id), ("transactions", plan.transaction_id)):
            with self.subTest(table=table):
                with UnitOfWork(self.config) as holder:
                    with holder.connection.cursor() as cursor:
                        cursor.execute(
                            sql.SQL("SELECT id FROM {} WHERE id = %s FOR UPDATE").format(
                                sql.Identifier(table),
                            ), (row_id,),
                        )
                    with self.assertRaises(psycopg2.errors.LockNotAvailable):
                        with UnitOfWork(self.config) as contender:
                            with contender.connection.cursor() as cursor:
                                cursor.execute("SET LOCAL lock_timeout = '100ms'")
                            PlanRepository(contender.connection).cancel(
                                plan.transaction_id, Decimal("1"), "Blocked",
                            )
        self.assertEqual(self.count_rows("plan_cancellations"), 0)

    def test_parallel_cancellations_cannot_exceed_plan(self):
        plan = self.create_plan()
        barrier = Barrier(2)

        def cancel():
            with UnitOfWork(self.config) as uow:
                with uow.connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '3s'")
                barrier.wait(timeout=5)
                try:
                    cancellation = PlanRepository(uow.connection).cancel(
                        plan.transaction_id, Decimal("70"), "Concurrent",
                    )
                except ValueError:
                    return None
                return cancellation

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(cancel) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(result is not None for result in results), 1)
        with UnitOfWork(self.config) as uow:
            current = PlanRepository(uow.connection).get_receipt(plan.id)
            self.assertEqual(current.cancelled_quantity_kg, Decimal("70"))
            self.assertEqual(current.remaining_quantity_kg, Decimal("30"))


if __name__ == "__main__":
    unittest.main()
