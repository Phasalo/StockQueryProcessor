import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from io import BytesIO
from os import environ, getenv
from pathlib import Path
from unittest.mock import Mock, patch
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
    CustomerRepository,
    FilmTypeRepository,
    MaterialRepository,
    SupplierRepository,
)
from database.repositories.movements import MovementRepository
from database.repositories.plans import PlanRepository
from database.repositories.reports import JournalRecord, ReportRepository
from database.repositories.transactions import TransactionRepository
from database.unit_of_work import UnitOfWork
from semantic_entry.core import Actor, CommandCatalog, CommandRegistry, QueryExecutor
from semantic_entry.modules.warehouse.models import StockBalance
from semantic_entry.modules.warehouse.postgres.stock import StockQueryHandler
from semantic_entry.modules.warehouse.queries import GET_STOCK_SPEC, GetStockResult


class ReportRepositoryPostgresTest(unittest.TestCase):
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
                cursor.execute(
                    "DELETE FROM materials WHERE id NOT IN (%s, %s)",
                    (self.material_id, self.other_material_id),
                )
                cursor.execute(
                    "DELETE FROM film_types WHERE code NOT IN "
                    "('BOPP', 'BOPP_PEARL', 'BOPP_METAL', 'CPP', 'LDPE_HALF_SLEEVE', 'LDPE_FLAT_SHEET')"
                )

    def journal(self):
        with UnitOfWork(self.config) as uow:
            return ReportRepository(uow.connection).list_transactions()

    def stock(self, material_id=None):
        with UnitOfWork(self.config) as uow:
            return ReportRepository(uow.connection).get_stock(
                self.material_id if material_id is None else material_id,
            )

    def stock_query(self, material_id):
        registry = CommandRegistry()
        registry.register(GET_STOCK_SPEC)
        authorizer = Mock()
        authorizer.is_allowed.return_value = True
        executor = QueryExecutor(CommandCatalog(registry, authorizer))
        executor.register(GET_STOCK_SPEC, StockQueryHandler(self.config))
        return executor.query("warehouse.get_stock", 1, {"material_id": material_id}, Actor("reader"))

    def test_stock_query_returns_typed_balances_for_one_material(self):
        with UnitOfWork(self.config) as uow:
            movements = MovementRepository(uow.connection)
            movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("100.25"))
            movements.add_issue(self.material_id, self.customer_id, None, Decimal("-20.05"))
            movements.add_receipt(self.other_material_id, self.supplier_id, self.actual_date, Decimal("7"))
            TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("-5.10"))
            plans = PlanRepository(uow.connection)
            outgoing = plans.add_consumption(self.material_id, self.customer_id, Decimal("-30.25"))
            incoming = plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("40.75"))
            for plan in (outgoing, incoming):
                plans.cancel(plan.transaction_id, Decimal("10"), "Partial")
        before = self.journal()
        result = self.stock_query(self.material_id)
        self.assertIsInstance(result, GetStockResult)
        stock = result.stock
        self.assertEqual((stock.material_id, stock.film_type_code, stock.brand), (self.material_id, "BOPP", "Brand"))
        self.assertEqual((stock.format, stock.thickness), (1200, 25))
        self.assertEqual(stock.on_hand_kg, Decimal("75.10"))
        self.assertEqual(stock.reserved_kg, Decimal("20.25"))
        self.assertEqual(stock.planned_receipt_kg, Decimal("30.75"))
        self.assertEqual(stock.available_kg, Decimal("54.85"))
        self.assertEqual(stock.projected_kg, Decimal("85.60"))
        self.assertIsInstance(stock.on_hand_kg, Decimal)
        self.assertEqual(result.model_dump(mode="json")["stock"]["available_kg"], "54.85")
        self.assertEqual(self.journal(), before)

    def test_stock_query_distinguishes_missing_material_from_zero_balance(self):
        stock = self.stock_query(self.material_id).stock
        self.assertEqual(stock.material_id, self.material_id)
        self.assertEqual(stock.on_hand_kg, Decimal("0"))
        self.assertEqual(stock.projected_kg, Decimal("0"))
        missing = self.stock_query(max(self.material_id, self.other_material_id) + 1)
        self.assertIsNone(missing.stock)
        self.assertEqual(missing.model_dump(mode="json"), {"stock": None})

    def test_stock_query_preserves_negative_balances(self):
        with UnitOfWork(self.config) as uow:
            TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("-2.30"))
        stock = self.stock_query(self.material_id).stock
        self.assertEqual(stock.on_hand_kg, Decimal("-2.30"))
        self.assertEqual(stock.available_kg, Decimal("-2.30"))
        self.assertEqual(stock.projected_kg, Decimal("-2.30"))

    def test_summary_facade_keeps_brands_separate_and_legacy_quantity_signs(self):
        with UnitOfWork(self.config) as uow:
            movements = MovementRepository(uow.connection)
            movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("100"))
            movements.add_issue(self.material_id, self.customer_id, None, Decimal("-20"))
            movements.add_receipt(self.other_material_id, self.supplier_id, self.actual_date, Decimal("7"))
            plans = PlanRepository(uow.connection)
            outgoing = plans.add_consumption(self.material_id, self.customer_id, Decimal("-30"))
            incoming = plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("40"))
            for plan in (outgoing, incoming):
                plans.cancel(plan.transaction_id, Decimal("10"), "Partial")
            plans.add_consumption(self.other_material_id, self.customer_id, Decimal("-200"))
        reports = WarehouseReports(self.config)
        rows = reports.get_film_type_summary(FilmType.BOPP)
        self.assertEqual([row["brand"] for row in rows], ["Brand", "Other"])
        self.assertEqual([row["free_balance_kg"] for row in rows], [Decimal("60"), Decimal("-193")])
        summary = reports.get_film_summary("BOPP", 1200, 25, brand="Brand")
        self.assertEqual(summary["material_id"], self.material_id)
        self.assertEqual(summary["on_hand_kg"], Decimal("80"))
        self.assertEqual(summary["reserved_kg"], Decimal("20"))
        self.assertEqual(summary["planned_consumption_kg"], Decimal("-20"))
        self.assertEqual(summary["planned_receipt_kg"], Decimal("30"))
        self.assertEqual(summary["free_balance_kg"], Decimal("60"))
        self.assertEqual(summary["projected_kg"], Decimal("90"))
        with localcontext() as context:
            context.prec = 1
            self.assertEqual(reports.get_film_summary("BOPP", 1200, 25, brand="Brand"), summary)
        for method in (reports.get_film_summary, reports.get_planned_consumption_details, reports.get_planned_receipt_details):
            with self.assertRaisesRegex(ValueError, "brand"):
                method(FilmType.BOPP, 1200, 25)

    def test_plan_details_match_open_totals_without_limit_or_closed_plans(self):
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            outgoing = plans.add_consumption(self.material_id, self.customer_id, Decimal("-80"))
            incoming = plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("100"))
            for plan in (outgoing, incoming):
                plans.cancel(plan.transaction_id, Decimal("10"), "First")
                plans.cancel(plan.transaction_id, Decimal("15"), "Second")
            cancelled = plans.add_consumption(self.material_id, self.customer_id, Decimal("-5"))
            plans.cancel(cancelled.transaction_id, Decimal("5"), "All")
            executed = plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("5"))
            MovementRepository(uow.connection).add_receipt(
                self.material_id, self.supplier_id, self.actual_date, Decimal("1"), planned_receipt_id=executed.id,
            )
            for _ in range(12):
                plans.add_consumption(self.material_id, self.customer_id, Decimal("-1"))
                plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("1"))
            plans.add_consumption(self.other_material_id, self.customer_id, Decimal("-500"))
        reports = WarehouseReports(self.config)
        outgoing_rows = reports.get_planned_consumption_details("BOPP", 1200, 25, brand="Brand")
        incoming_rows = reports.get_planned_receipt_details("BOPP", 1200, 25, brand="Brand")
        self.assertEqual(len(outgoing_rows), 13)
        self.assertEqual(len(incoming_rows), 13)
        self.assertEqual(outgoing_rows[0]["plan_id"], outgoing.id)
        self.assertEqual(outgoing_rows[0]["transaction_id"], outgoing.transaction_id)
        self.assertEqual(outgoing_rows[0]["prod_card_number"], "000123")
        self.assertEqual(outgoing_rows[0]["delta_kg"], Decimal("-55"))
        self.assertEqual(incoming_rows[0]["plan_id"], incoming.id)
        self.assertEqual(incoming_rows[0]["order_date"], self.order_date)
        self.assertEqual(incoming_rows[0]["planned_date"], self.planned_date)
        self.assertEqual(incoming_rows[0]["delta_kg"], Decimal("75"))
        summary = reports.get_film_summary("BOPP", 1200, 25, brand="Brand")
        self.assertEqual(sum(row["delta_kg"] for row in outgoing_rows), summary["planned_consumption_kg"])
        self.assertEqual(sum(row["delta_kg"] for row in incoming_rows), summary["planned_receipt_kg"])

    def test_missing_material_combination_returns_empty_reports(self):
        reports = WarehouseReports(self.config)
        self.assertTrue(all(value == Decimal("0") for value in reports.get_film_summary("BOPP", 999, 999).values()))
        self.assertEqual(reports.get_planned_consumption_details("BOPP", 999, 999), [])
        self.assertEqual(reports.get_planned_receipt_details("BOPP", 999, 999), [])
        self.assertEqual(reports.get_film_type_summary("MISSING"), [])
        self.assertEqual(reports.get_available_formats("MISSING"), [])
        self.assertEqual(reports.get_available_thicknesses("MISSING"), [])

    def test_size_and_brand_lists_are_distinct_and_empty_brand_is_selectable(self):
        with UnitOfWork(self.config) as uow:
            film_type = FilmTypeRepository(uow.connection).find_by_code("BOPP")
            materials = MaterialRepository(uow.connection)
            materials.add(film_type.id, "A&B / <tag>", 1400, 30)
            empty_brand = materials.add(film_type.id, "", 1500, 40)
            materials.add(film_type.id, "Named", 1500, 40)
        reports = WarehouseReports(self.config)
        self.assertEqual(reports.get_available_formats(FilmType.BOPP), [1200, 1400, 1500])
        self.assertEqual(reports.get_available_thicknesses("BOPP"), [25, 30, 40])
        self.assertEqual(reports.get_available_brands("BOPP", 1200, 25), ["Brand", "Other"])
        self.assertEqual(reports.get_available_brands("BOPP", 1400, 30), ["A&B / <tag>"])
        self.assertEqual(reports.get_film_summary("BOPP", 1500, 40, brand="")["material_id"], empty_brand)
        with patch("app.views.reports", reports):
            self.assertEqual(app.test_client().get("/film-type/BOPP/1500/40?brand=").status_code, 200)

    def test_summary_detail_and_exports_read_real_database_for_selected_brand(self):
        with UnitOfWork(self.config) as uow:
            movements = MovementRepository(uow.connection)
            movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("100"))
            movements.add_receipt(self.other_material_id, self.supplier_id, self.actual_date, Decimal("7"))
            PlanRepository(uow.connection).add_consumption(self.material_id, self.customer_id, Decimal("-20"))
        reports = WarehouseReports(self.config)
        with patch("app.views.reports", reports):
            client = app.test_client()
            summary = client.get("/film-type/BOPP")
            self.assertEqual(summary.status_code, 200)
            self.assertIn("?brand=Brand", summary.text)
            self.assertIn("?brand=Other", summary.text)
            self.assertIn("Выберите марку", client.get("/film-type/BOPP/1200/25").text)
            detail = client.get("/film-type/BOPP/1200/25?brand=Brand")
            self.assertEqual(detail.status_code, 200)
            self.assertIn("80,00 кг", detail.text)
            self.assertIn("Физический остаток", detail.text)
            self.assertEqual(client.get("/exports/film-type/BOPP/1200/25").status_code, 400)
            response = client.get("/exports/film-type/BOPP/1200/25?brand=Brand")
            self.assertEqual(response.status_code, 200)
            book = load_workbook(BytesIO(response.data))
            self.assertEqual(book["Сводка"]["D1"].value, "Марка: Brand")
            self.assertEqual(book["Сводка"]["B4"].value, 80)
            self.assertEqual(book["План расхода"]["C4"].value, -20)
            response = client.get("/exports/film-type/BOPP")
            self.assertEqual(response.status_code, 200)
            sheet = load_workbook(BytesIO(response.data)).active
            self.assertEqual([sheet["A4"].value, sheet["A5"].value], ["Brand", "Other"])
            self.assertEqual([sheet["F4"].value, sheet["F5"].value], [80, 7])

    def test_stock_for_material_without_transactions_has_decimal_zeros(self):
        record = self.stock()
        self.assertIsInstance(record, StockBalance)
        self.assertEqual(record.material_id, self.material_id)
        self.assertEqual(record.film_type_code, "BOPP")
        self.assertEqual(record.brand, "Brand")
        for field in ("on_hand_kg", "reserved_kg", "planned_receipt_kg", "available_kg", "projected_kg"):
            self.assertIsInstance(getattr(record, field), Decimal)
            self.assertEqual(getattr(record, field), Decimal("0"))
        with UnitOfWork(self.config) as uow:
            self.assertIsNone(ReportRepository(uow.connection).get_stock(999999))

    def test_stock_uses_only_facts_and_adjustments_for_physical_balance(self):
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            plans.add_consumption(self.material_id, self.customer_id, Decimal("-50"))
            plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("500"))
            movements = MovementRepository(uow.connection)
            movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("100.25"))
            movements.add_issue(self.material_id, self.customer_id, self.actual_date, Decimal("-30.10"))
            journal = TransactionRepository(uow.connection)
            journal.add("ADJUSTMENT", self.material_id, Decimal("-5"))
            journal.add("ADJUSTMENT", self.material_id, Decimal("2"))
        record = self.stock()
        self.assertEqual(record.on_hand_kg, Decimal("67.15"))
        self.assertEqual(record.reserved_kg, Decimal("50"))
        self.assertEqual(record.planned_receipt_kg, Decimal("500"))
        self.assertEqual(record.available_kg, Decimal("17.15"))
        self.assertEqual(record.projected_kg, Decimal("517.15"))

    def test_stock_aggregates_multiple_plans_facts_and_cancellations_once(self):
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            outgoing = plans.add_consumption(self.material_id, self.customer_id, Decimal("-80"))
            plans.add_consumption(self.material_id, self.customer_id, Decimal("-10"))
            incoming = plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("100"))
            plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("5"))
            for plan in (incoming, outgoing):
                plans.cancel(plan.transaction_id, Decimal("10"), "First")
                plans.cancel(plan.transaction_id, Decimal("15"), "Second")
            movements = MovementRepository(uow.connection)
            movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("30"))
            movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("10"))
            record = ReportRepository(uow.connection).get_stock(self.material_id)
            self.assertEqual(record.reserved_kg, sum(plan.remaining_quantity_kg for plan in plans.find_open_consumptions()))
            self.assertEqual(record.planned_receipt_kg, sum(plan.remaining_quantity_kg for plan in plans.find_open_receipts()))
        self.assertEqual(record.on_hand_kg, Decimal("40"))
        self.assertEqual(record.reserved_kg, Decimal("65"))
        self.assertEqual(record.planned_receipt_kg, Decimal("80"))
        self.assertEqual(record.available_kg, Decimal("-25"))
        self.assertEqual(record.projected_kg, Decimal("55"))

    def test_stock_excludes_fully_cancelled_plans(self):
        with UnitOfWork(self.config) as uow:
            plans = PlanRepository(uow.connection)
            outgoing = plans.add_consumption(self.material_id, self.customer_id, Decimal("-80"))
            incoming = plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("100"))
            plans.cancel(outgoing.transaction_id, Decimal("80"), "All")
            plans.cancel(incoming.transaction_id, Decimal("100"), "All")
        record = self.stock()
        self.assertEqual(record.on_hand_kg, Decimal("0"))
        self.assertEqual(record.reserved_kg, Decimal("0"))
        self.assertEqual(record.planned_receipt_kg, Decimal("0"))
        self.assertEqual(record.available_kg, Decimal("0"))
        self.assertEqual(record.projected_kg, Decimal("0"))

    def test_stock_excludes_entire_executed_plan_for_smaller_and_larger_facts(self):
        total = Decimal("0")
        for quantity in (Decimal("1"), Decimal("150")):
            with self.subTest(quantity=quantity):
                with UnitOfWork(self.config) as uow:
                    plans = PlanRepository(uow.connection)
                    outgoing = plans.add_consumption(self.other_material_id, self.customer_id, Decimal("-80"))
                    incoming = plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("100"))
                    for plan in (incoming, outgoing):
                        plans.cancel(plan.transaction_id, Decimal("30"), "Partial")
                    movements = MovementRepository(uow.connection)
                    movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, quantity, planned_receipt_id=incoming.id)
                    movements.add_issue(self.other_material_id, self.customer_id, self.actual_date, quantity.copy_negate(), planned_consumption_id=outgoing.id)
                total += quantity
                incoming_stock = self.stock()
                outgoing_stock = self.stock(self.other_material_id)
                self.assertEqual(incoming_stock.on_hand_kg, total)
                self.assertEqual(outgoing_stock.on_hand_kg, -total)
                for record in (incoming_stock, outgoing_stock):
                    self.assertEqual(record.reserved_kg, Decimal("0"))
                    self.assertEqual(record.planned_receipt_kg, Decimal("0"))
                    self.assertEqual(record.available_kg, record.on_hand_kg)
                    self.assertEqual(record.projected_kg, record.on_hand_kg)

    def test_stock_keeps_brands_separate_and_does_not_clamp_negative_balances(self):
        with UnitOfWork(self.config) as uow:
            movements = MovementRepository(uow.connection)
            movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("20"))
            movements.add_issue(self.other_material_id, self.customer_id, None, Decimal("-7"))
            PlanRepository(uow.connection).add_consumption(self.material_id, self.customer_id, Decimal("-50"))
            rows = ReportRepository(uow.connection).list_stock("BOPP")
        self.assertEqual([row.material_id for row in rows], [self.material_id, self.other_material_id])
        self.assertEqual([row.brand for row in rows], ["Brand", "Other"])
        self.assertEqual([row.on_hand_kg for row in rows], [Decimal("20"), Decimal("-7")])
        self.assertEqual([row.available_kg for row in rows], [Decimal("-30"), Decimal("-7")])
        self.assertEqual([row.projected_kg for row in rows], [Decimal("-30"), Decimal("-7")])

    def test_stock_filter_supports_custom_types_and_binds_the_code(self):
        code = "STOCK'; DROP TABLE materials; --"
        with UnitOfWork(self.config) as uow:
            film_type_id = FilmTypeRepository(uow.connection).add(code, "Отдельный тип")
            material_id = MaterialRepository(uow.connection).add(film_type_id, "New", 1500, 30)
            reports = ReportRepository(uow.connection)
            row, = reports.list_stock(code)
            self.assertEqual(row.material_id, material_id)
            self.assertEqual(row.film_type_code, code)
            self.assertEqual(row.film_type_name, "Отдельный тип")
            self.assertEqual(reports.list_stock("MISSING"), [])
            self.assertGreaterEqual(len(reports.list_stock()), 3)

    def test_stock_decimal_sums_are_exact_and_can_exceed_single_entry_range(self):
        with UnitOfWork(self.config) as uow:
            movements = MovementRepository(uow.connection)
            for _ in range(2):
                movements.add_receipt(self.material_id, self.supplier_id, self.actual_date, Decimal("99999999.99"))
            plans = PlanRepository(uow.connection)
            outgoing = plans.add_consumption(self.material_id, self.customer_id, Decimal("-99999999.99"))
            incoming = plans.add_receipt(self.material_id, self.supplier_id, self.order_date, self.planned_date, Decimal("1.01"))
            for plan in (outgoing, incoming):
                plans.cancel(plan.transaction_id, Decimal("0.01"), "Partial")
        with localcontext() as context:
            context.prec = 2
            record = self.stock()
        self.assertEqual(record.on_hand_kg, Decimal("199999999.98"))
        self.assertEqual(record.reserved_kg, Decimal("99999999.98"))
        self.assertEqual(record.planned_receipt_kg, Decimal("1"))
        self.assertEqual(record.available_kg, Decimal("100000000.00"))
        self.assertEqual(record.projected_kg, Decimal("100000001.00"))

    def test_stock_uses_callers_transaction_without_committing(self):
        with UnitOfWork(self.config) as uow:
            TransactionRepository(uow.connection).add("ADJUSTMENT", self.material_id, Decimal("5"))
            self.assertEqual(ReportRepository(uow.connection).get_stock(self.material_id).on_hand_kg, Decimal("5"))
            self.assertEqual(self.stock().on_hand_kg, Decimal("0"))
            uow.rollback()
        self.assertEqual(self.stock().on_hand_kg, Decimal("0"))

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
                cursor.execute("UPDATE transactions SET created_at = %s", (datetime(2026, 11, 1, tzinfo=UTC),))
                cursor.execute(
                    "UPDATE transactions SET created_at = %s WHERE id = %s",
                    (datetime(2026, 11, 2, tzinfo=UTC), ids[0]),
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
        for row, material_id, quantity in zip(rows, (self.other_material_id, self.material_id), (Decimal("20"), Decimal("10")), strict=True):
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
