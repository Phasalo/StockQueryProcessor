from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from os import environ, getenv
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from openpyxl import load_workbook
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from app import app
from config import WarehouseConfig
from database.connection import create_connection
from database.enums import FilmType, TransactionType
from database.reports import WarehouseReports
from database.repositories.catalog import (
    CustomerRepository, FilmTypeRepository, MaterialRepository, SupplierRepository,
)
from database.repositories.movements import MovementRepository
from database.repositories.plans import PlanRepository
from database.repositories.reports import JournalRecord, ReportRepository
from database.repositories.transactions import TransactionRepository
from database.unit_of_work import UnitOfWork


class JournalPostgresTest(unittest.TestCase):
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
        cls.config = replace(cls.admin_config, database="reports_test_" + uuid4().hex)
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
            ], env=env, capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        with UnitOfWork(cls.config) as uow:
            film_type = FilmTypeRepository(uow.connection).find_by_code("BOPP")
            materials = MaterialRepository(uow.connection)
            cls.material_id = materials.add(film_type.id, "Brand", 1200, 25)
            cls.other_material_id = materials.add(film_type.id, "Other", 1200, 25)
            cls.customer_id = CustomerRepository(uow.connection).add("Заказчик", "000123")
            cls.supplier_id = SupplierRepository(uow.connection).add("Поставщик")

    @classmethod
    def drop_database(cls):
        with closing(create_connection(cls.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.config.database)))

    def setUp(self):
        self.order_date = date(2026, 11, 1)
        self.planned_date = date(2026, 11, 5)
        self.actual_date = date(2026, 11, 10)
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("TRUNCATE transactions RESTART IDENTITY CASCADE")

    def journal(self):
        with UnitOfWork(self.config) as uow:
            return ReportRepository(uow.connection).list_transactions()

    def test_empty_journal_does_not_include_materials_without_transactions(self):
        self.assertEqual(self.journal(), [])
        self.assertEqual(WarehouseReports(self.config).get_all_transactions(), [])

    def test_all_types_keep_original_quantities_and_do_not_multiply_cancellations(self):
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            incoming = plans.add_receipt(
                self.material_id, self.supplier_id, self.order_date,
                self.planned_date, Decimal("100"),
            )
            outgoing = plans.add_consumption(self.material_id, self.customer_id, Decimal("-80"))
            for plan in (incoming, outgoing):
                plans.cancel(plan.transaction_id, Decimal("10"), "First")
                plans.cancel(plan.transaction_id, Decimal("15"), "Second")
            movements = MovementRepository(uow.connection)
            receipt = movements.add_receipt(
                self.material_id, self.supplier_id, self.actual_date, Decimal("150"),
                planned_receipt_id=incoming.id,
            )
            issue = movements.add_issue(
                self.material_id, self.customer_id, self.actual_date, Decimal("-90"),
                planned_consumption_id=outgoing.id,
            )
            adjustment = TransactionRepository(uow.connection).add(
                "ADJUSTMENT", self.material_id, Decimal("-5"),
            )
        rows = self.journal()
        self.assertEqual(len(rows), 5)
        self.assertEqual([row.transaction_id for row in rows], sorted(
            [incoming.transaction_id, outgoing.transaction_id, receipt.transaction_id,
             issue.transaction_id, adjustment.id], reverse=True,
        ))
        by_type = {row.transaction_type: row for row in rows}
        self.assertEqual(set(by_type), set(TransactionType.get_values()))
        self.assertEqual(by_type["CONSUMPTION"].quantity_delta_kg, Decimal("-80"))
        self.assertEqual(by_type["PLANNED_RECEIPT"].quantity_delta_kg, Decimal("100"))
        self.assertEqual(by_type["ISSUE"].quantity_delta_kg, Decimal("-90"))
        self.assertEqual(by_type["RECEIPT"].quantity_delta_kg, Decimal("150"))
        for row in rows:
            self.assertIsInstance(row, JournalRecord)
            self.assertIsInstance(row.quantity_delta_kg, Decimal)
            self.assertIsNotNone(row.created_at.utcoffset())
            self.assertEqual(row.material_id, self.material_id)
            self.assertEqual(row.brand, "Brand")
        for kind in ("CONSUMPTION", "ISSUE"):
            self.assertEqual(by_type[kind].customer_name, "Заказчик")
            self.assertEqual(by_type[kind].customer_id, self.customer_id)
            self.assertEqual(by_type[kind].customer_prod_card_number, "000123")
            self.assertIsNone(by_type[kind].supplier_name)
            self.assertIsNone(by_type[kind].planned_date)
        for kind in ("PLANNED_RECEIPT", "RECEIPT"):
            self.assertEqual(by_type[kind].supplier_name, "Поставщик")
            self.assertEqual(by_type[kind].supplier_id, self.supplier_id)
            self.assertEqual(by_type[kind].order_date, self.order_date)
            self.assertEqual(by_type[kind].planned_date, self.planned_date)
            self.assertIsNone(by_type[kind].customer_name)
        for kind in ("RECEIPT", "ISSUE"):
            self.assertEqual(by_type[kind].actual_date, self.actual_date)
        for kind in ("CONSUMPTION", "PLANNED_RECEIPT", "ADJUSTMENT"):
            self.assertIsNone(by_type[kind].actual_date)
        self.assertIsNone(by_type["ADJUSTMENT"].supplier_name)
        self.assertIsNone(by_type["ADJUSTMENT"].customer_name)

    def test_unplanned_facts_use_own_parties_and_preserve_absent_dates(self):
        with UnitOfWork(self.config) as uow:
            movements = MovementRepository(uow.connection)
            movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("1"))
            movements.add_issue(self.material_id, self.customer_id, None, Decimal("-2"))
        issue, receipt = self.journal()
        self.assertEqual(issue.customer_name, "Заказчик")
        self.assertEqual(issue.customer_prod_card_number, "000123")
        self.assertIsNone(issue.actual_date)
        self.assertEqual(receipt.supplier_name, "Поставщик")
        self.assertEqual(receipt.actual_date, self.actual_date)
        self.assertIsNone(receipt.planned_date)
        self.assertIsNone(receipt.order_date)

    def test_order_uses_created_at_then_id_descending(self):
        with UnitOfWork(self.config) as uow:
            journal = TransactionRepository(uow.connection)
            ids = [journal.add("ADJUSTMENT", self.material_id, Decimal("1")).id for _ in range(3)]
            with uow.connection.cursor() as cursor:
                cursor.execute("UPDATE transactions SET created_at = %s", (datetime(2026, 11, 1, tzinfo=timezone.utc),))
                cursor.execute(
                    "UPDATE transactions SET created_at = %s WHERE id = %s",
                    (datetime(2026, 11, 2, tzinfo=timezone.utc), ids[0]),
                )
        self.assertEqual([row.transaction_id for row in self.journal()], [ids[0], ids[2], ids[1]])

    def test_brands_remain_separate_and_facade_preserves_legacy_fields(self):
        with UnitOfWork(self.config) as uow:
            movements = MovementRepository(uow.connection)
            first = movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("10"))
            second = movements.add_receipt(self.other_material_id, self.supplier_id, self.actual_date, Decimal("20"))
        rows = WarehouseReports(self.config).get_all_transactions()
        self.assertEqual([row["id"] for row in rows], [second.transaction_id, first.transaction_id])
        self.assertEqual([row["film_brand"] for row in rows], ["Other", "Brand"])
        for row, material_id, quantity in zip(rows, (self.other_material_id, self.material_id), (Decimal("20"), Decimal("10"))):
            self.assertEqual(row["material_id"], material_id)
            self.assertEqual(row["film_type"], FilmType.BOPP)
            self.assertEqual(row["transaction_type"], TransactionType.RECEIPT)
            self.assertEqual(row["film_type_name"], FilmType.BOPP.value)
            self.assertEqual(row["film_format"], 1200)
            self.assertEqual(row["film_thickness"], 25)
            self.assertEqual(row["film_delta_kg"], quantity)
            self.assertIsNone(row["customer_name"])
            self.assertIsNone(row["customer_prod_card_number"])
            self.assertEqual(row["supplier_name"], "Поставщик")
            self.assertIsNone(row["planned_date"])
            self.assertEqual(row["actual_date"], self.actual_date)

    def test_report_uses_callers_transaction_without_committing(self):
        reports = WarehouseReports(self.config)
        with UnitOfWork(self.config) as uow:
            transaction = TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("1"))
            self.assertEqual([row.transaction_id for row in ReportRepository(uow.connection).list_transactions()], [transaction.id])
            self.assertEqual(reports.get_all_transactions(), [])
            uow.rollback()
        self.assertEqual(reports.get_all_transactions(), [])

    def test_detail_from_wrong_transaction_type_does_not_leak_into_journal(self):
        with UnitOfWork(self.config) as uow:
            transaction = TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("1"))
            with uow.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO consumption (transaction_id, customer_id) VALUES (%s, %s)",
                    (transaction.id, self.customer_id),
                )
        row, = self.journal()
        self.assertEqual(row.transaction_type, "ADJUSTMENT")
        self.assertIsNone(row.customer_id)
        self.assertIsNone(row.customer_name)

    def test_custom_type_issue_is_rendered_and_exported_with_customer_and_date(self):
        with UnitOfWork(self.config) as uow:
            film_type_id = FilmTypeRepository(uow.connection).add("CUSTOM", "Особая плёнка")
            material_id = MaterialRepository(uow.connection).add(film_type_id, "Новая марка", 1200, 25)
            MovementRepository(uow.connection).add_issue(material_id, self.customer_id, self.actual_date, Decimal("-2"))
        reports = WarehouseReports(self.config)
        row, = reports.get_all_transactions()
        self.assertEqual(row["film_type"], "CUSTOM")
        self.assertEqual(row["film_type_name"], "Особая плёнка")
        with patch("app.views.reports", reports):
            html = app.test_client().get("/transactions")
            export = app.test_client().get("/exports/transactions")
        self.assertEqual(html.status_code, 200)
        self.assertIn("Особая плёнка", html.text)
        self.assertIn("Фактический расход", html.text)
        self.assertIn("Заказчик", html.text)
        self.assertIn("000123", html.text)
        self.assertIn("10.11.2026", html.text)
        self.assertEqual(export.status_code, 200)
        sheet = load_workbook(BytesIO(export.data)).active
        self.assertEqual(sheet["C3"].value, "Особая плёнка")
        self.assertEqual(sheet["G3"].value, -2)
        self.assertEqual(sheet["H3"].value, "Заказчик")
        self.assertEqual(sheet["I3"].value, "000123")
        self.assertEqual(sheet["K3"].value, "10.11.2026")


if __name__ == "__main__":
    unittest.main()
