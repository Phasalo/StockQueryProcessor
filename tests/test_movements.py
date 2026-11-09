from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import date
from decimal import Decimal
from os import environ, getenv
from pathlib import Path
import subprocess
import sys
from threading import Barrier
import unittest
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from config import WarehouseConfig
from database.connection import create_connection
from database.enums import TransactionType
from database.repositories.catalog import (
    CustomerRepository, FilmTypeRepository, MaterialRepository, SupplierRepository,
)
from database.repositories.movements import IssueRecord, MovementRepository, ReceiptRecord
from database.repositories.plans import PlanRepository
from database.repositories.transactions import TransactionRepository
from database.unit_of_work import UnitOfWork


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

            def execute():
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

            def perform(operation):
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
