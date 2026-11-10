import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from os import environ, getenv
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from pydantic import ValidationError

from semantic_entry.adapters.memory.drafts import MemoryDraftStore
from semantic_entry.adapters.postgres.config import PostgresConfig
from semantic_entry.adapters.postgres.connection import create_connection
from semantic_entry.adapters.postgres.unit_of_work import UnitOfWork
from semantic_entry.core.access import Actor, PermissionDeniedError
from semantic_entry.core.drafts.models import DraftState
from semantic_entry.modules.warehouse.bootstrap import build_warehouse_engine
from semantic_entry.modules.warehouse.models import MaterialRecord, SupplierRecord
from semantic_entry.modules.warehouse.postgres.repositories.catalog import (
    FilmTypeRepository,
    MaterialRepository,
    SupplierRepository,
)
from semantic_entry.modules.warehouse.queries.receipt_resolution import (
    RESOLVE_RECEIPT_SPEC,
    ResolutionStatus,
    ResolveReceiptResult,
)


class ReceiptResolutionContractTest(unittest.TestCase):
    def setUp(self):
        self.factory = Mock(side_effect=AssertionError("Unexpected transaction"))
        self.authorizer = Mock()
        self.authorizer.is_allowed.return_value = True
        self.actor = Actor("alice")
        self.engine = build_warehouse_engine(self.factory, self.authorizer, drafts=MemoryDraftStore())

    def resolve(self, payload):
        return self.engine.query("warehouse.resolve_receipt", 1, payload, self.actor)

    def test_missing_criteria_return_missing_references_without_connection(self):
        result = self.resolve({})
        self.assertEqual(result.material.status, ResolutionStatus.MISSING)
        self.assertEqual(result.supplier.status, ResolutionStatus.MISSING)
        self.assertEqual(result.resolved_ids, {})
        self.factory.assert_not_called()

    def test_invalid_criteria_are_rejected_before_connection(self):
        for payload in (
            {"material": None},
            {"material": {"film_type_code": " "}},
            {"material": {"format": True}},
            {"material": {"format": "1200"}},
            {"material": {"format": 0}},
            {"material": {"thickness": -1}},
            {"material": {"brand": 123}},
            {"material": {"material_id": 1}},
            {"supplier_name": " "},
            {"supplier_name": 123},
            {"limit": 0},
            {"limit": 101},
            {"limit": True},
            {"confirmed": True},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                self.resolve(payload)
        self.factory.assert_not_called()

    def test_receipt_permission_is_checked_before_validation_and_search(self):
        self.authorizer.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.resolve({"limit": 0})
        self.authorizer.is_allowed.assert_called_once_with(self.actor, "warehouse.receipt.write")
        self.factory.assert_not_called()

    def test_revoked_permission_is_checked_again(self):
        self.resolve({})
        self.authorizer.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.resolve({"supplier_name": "Acme"})
        self.factory.assert_not_called()

    def test_resolution_cannot_be_prepared_as_a_write(self):
        with self.assertRaisesRegex(ValueError, "WRITE"):
            self.engine.prepare("warehouse.resolve_receipt", 1, {}, self.actor, lifetime=timedelta(minutes=5))
        self.factory.assert_not_called()


class ReceiptResolutionPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = getenv("TEST_DATABASE_DSN")
        if not dsn:
            raise unittest.SkipTest("Set TEST_DATABASE_DSN to run PostgreSQL tests")
        settings = parse_dsn(dsn)
        cls.admin_config = PostgresConfig(
            database=settings["dbname"], user=settings["user"], password=settings.get("password", ""),
            host=settings["host"], port=int(settings.get("port", 5432)),
        )
        cls.config = replace(cls.admin_config, database="resolution_test_" + uuid4().hex)
        with closing(create_connection(cls.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.config.database)))
        cls.addClassCleanup(cls.drop_database)
        env = environ.copy()
        env.update(PGDATABASE=cls.config.database, PGUSER=cls.config.user, PGPASSWORD=cls.config.password, PGHOST=cls.config.host, PGPORT=str(cls.config.port))
        result = subprocess.run(
            [sys.executable, "-m", "pgmigrate", "-d", str(Path(__file__).resolve().parent.parent), "-t", "latest", "--check_serial_versions", "migrate"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        with UnitOfWork(cls.config) as uow:
            types = FilmTypeRepository(uow.connection)
            cls.bopp = types.find_by_code("BOPP").id
            cls.cpp = types.find_by_code("CPP").id
            cls.custom = types.add("CUSTOM", "Другой тип")

    @classmethod
    def drop_database(cls):
        with closing(create_connection(cls.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.config.database)))

    def setUp(self):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("TRUNCATE engine.executions, transactions, materials, suppliers RESTART IDENTITY CASCADE")
            materials = MaterialRepository(uow.connection)
            self.material_id = materials.add(self.bopp, "Brand", 1200, 25)
            self.other_material_id = materials.add(self.bopp, "Other", 1200, 25)
            materials.add(self.bopp, "Brand Plus", 1200, 25)
            materials.add(self.bopp, "Brand", 1400, 25)
            materials.add(self.bopp, "Brand", 1200, 30)
            materials.add(self.cpp, "Brand", 1200, 25)
            self.empty_brand_id = materials.add(self.bopp, "", 1200, 25)
            self.custom_material_id = materials.add(self.custom, "Custom", 1500, 40)
            suppliers = SupplierRepository(uow.connection)
            self.supplier_id = suppliers.add("Acme")
            suppliers.add("Acme Europe")
            self.partner_one_id = suppliers.add("Partner One")
            self.partner_two_id = suppliers.add("Partner Two")
        self.actor = Actor("alice")
        self.authorizer = Mock()
        self.authorizer.is_allowed.return_value = True
        self.engine = build_warehouse_engine(lambda: UnitOfWork(self.config), self.authorizer, drafts=MemoryDraftStore())
        self.material_lookup = {"film_type_code": "BOPP", "brand": "Brand", "format": 1200, "thickness": 25}

    def resolve(self, payload):
        return self.engine.query("warehouse.resolve_receipt", 1, payload, self.actor)

    def counts(self):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("SELECT (SELECT count(*) FROM materials), (SELECT count(*) FROM suppliers), (SELECT count(*) FROM transactions), (SELECT count(*) FROM receipts), (SELECT count(*) FROM engine.executions), (SELECT count(*) FROM engine.idempotency_records), (SELECT count(*) FROM engine.audit_events)")
                return cursor.fetchone()

    def test_exact_characteristics_and_supplier_name_resolve_to_ids(self):
        result = self.resolve({"material": self.material_lookup, "supplier_name": "Acme"})
        self.assertIsInstance(result, ResolveReceiptResult)
        self.assertEqual(result.resolved_ids, {"material_id": self.material_id, "supplier_id": self.supplier_id})
        self.assertEqual(result.material.candidates, (MaterialRecord(self.material_id, self.bopp, "BOPP", "Brand", 1200, 25),))
        self.assertEqual(result.supplier.candidates, (SupplierRecord(self.supplier_id, "Acme"),))
        self.assertFalse(result.material.has_more)
        self.assertFalse(result.supplier.has_more)
        restored = RESOLVE_RECEIPT_SPEC.validate_result(result.model_dump(mode="json", round_trip=True))
        self.assertEqual(restored, result)

    def test_partial_characteristics_return_all_matching_candidates(self):
        result = self.resolve({"material": {"film_type_code": "BOPP", "format": 1200, "thickness": 25}})
        self.assertEqual(result.material.status, ResolutionStatus.AMBIGUOUS)
        self.assertEqual([row.brand for row in result.material.candidates], ["Brand", "Other", "Brand Plus", ""])
        self.assertIsNone(result.material.resolved_id)
        self.assertEqual(result.resolved_ids, {})
        self.assertEqual(result.supplier.status, ResolutionStatus.MISSING)

    def test_single_visible_candidate_with_more_matches_is_not_resolved(self):
        result = self.resolve({"material": {"film_type_code": "BOPP"}, "supplier_name": "Partner", "limit": 1})
        for reference in (result.material, result.supplier):
            with self.subTest(reference=reference):
                self.assertEqual(reference.status, ResolutionStatus.AMBIGUOUS)
                self.assertEqual(len(reference.candidates), 1)
                self.assertTrue(reference.has_more)
                self.assertIsNone(reference.resolved_id)
        self.assertEqual(result.resolved_ids, {})
        self.assertEqual(result.material.candidates[0].id, self.material_id)
        self.assertEqual(result.supplier.candidates[0].id, self.partner_one_id)

    def test_unique_supplier_fragment_is_case_insensitive(self):
        result = self.resolve({"supplier_name": "pArTnEr oN"})
        self.assertEqual(result.resolved_ids, {"supplier_id": self.partner_one_id})
        self.assertEqual(result.material.status, ResolutionStatus.MISSING)

    def test_unknown_values_are_not_substituted_with_other_records(self):
        for material in (
            {"film_type_code": "UNKNOWN"},
            {"brand": "Missing"},
            {"format": 9999},
            {**self.material_lookup, "brand": "Bra"},
        ):
            with self.subTest(material=material):
                result = self.resolve({"material": material, "supplier_name": "Missing"})
                self.assertEqual(result.material.status, ResolutionStatus.NOT_FOUND)
                self.assertEqual(result.supplier.status, ResolutionStatus.NOT_FOUND)
                self.assertEqual(result.resolved_ids, {})

    def test_explicit_empty_brand_does_not_mean_any_brand(self):
        result = self.resolve({"material": {"brand": ""}})
        self.assertEqual(result.material.resolved_id, self.empty_brand_id)

    def test_custom_film_type_is_resolved_from_database(self):
        result = self.resolve({"material": {"film_type_code": "CUSTOM"}})
        self.assertEqual(result.material.resolved_id, self.custom_material_id)

    def test_supplier_pattern_characters_are_matched_literally(self):
        with UnitOfWork(self.config) as uow:
            suppliers = SupplierRepository(uow.connection)
            literal_id = suppliers.add("North %_\\ Supplies")
            suppliers.add("North ABx Supplies")
        result = self.resolve({"supplier_name": "%_\\"})
        self.assertEqual(result.supplier.resolved_id, literal_id)
        with UnitOfWork(self.config) as uow:
            self.assertEqual([row.id for row in SupplierRepository(uow.connection).search("%_\\", literal=True)], [literal_id])

    def test_material_brand_is_literal_and_query_values_are_bound(self):
        brand = "B%_\\rand'; DROP TABLE materials; --"
        with UnitOfWork(self.config) as uow:
            material_id = MaterialRepository(uow.connection).add(self.bopp, brand, 1200, 25)
        result = self.resolve({"material": {"brand": brand}})
        self.assertEqual(result.material.resolved_id, material_id)
        self.assertEqual(self.resolve({"material": self.material_lookup}).material.resolved_id, self.material_id)

    def test_resolution_does_not_write_catalog_movements_or_execution_ledger(self):
        before = self.counts()
        for payload in ({}, {"material": self.material_lookup, "supplier_name": "Acme"}, {"material": {"brand": "Missing"}}, {"supplier_name": "Partner"}):
            self.resolve(payload)
        self.assertEqual(self.counts(), before)

    def test_resolved_ids_enter_existing_prepare_and_confirm_flow(self):
        resolved = self.resolve({"material": self.material_lookup, "supplier_name": "Acme"})
        before = self.counts()
        draft = self.engine.prepare(
            "warehouse.record_receipt", 1,
            {**resolved.resolved_ids, "quantity_kg": "12.50", "actual_date": "2026-11-10"},
            self.actor, lifetime=timedelta(minutes=10),
        )
        self.assertEqual(draft.state, DraftState.READY)
        self.assertEqual(self.counts(), before)
        result = self.engine.confirm(draft.draft_id, draft.version, "receipt-1", self.actor)
        self.assertEqual(self.counts()[2:], (1, 1, 1, 1, 1))
        self.assertEqual(self.engine.confirm(draft.draft_id, draft.version, "receipt-1", self.actor), result)
        stock = self.engine.query("warehouse.get_stock", 1, {"material_id": self.material_id}, self.actor).stock
        self.assertEqual(stock.on_hand_kg, Decimal("12.50"))

    def test_ambiguous_candidates_require_explicit_ids_before_ready_preview(self):
        resolved = self.resolve({"material": {"film_type_code": "BOPP", "format": 1200, "thickness": 25}, "supplier_name": "Partner"})
        draft = self.engine.prepare(
            "warehouse.record_receipt", 1,
            {**resolved.resolved_ids, "quantity_kg": "1", "actual_date": "2026-11-10"},
            self.actor, lifetime=timedelta(minutes=10),
        )
        self.assertEqual(draft.state, DraftState.NEEDS_INPUT)
        material = resolved.material.candidates[-1]
        supplier = resolved.supplier.candidates[-1]
        revised = self.engine.revise(draft.draft_id, draft.version, {"material_id": material.id, "supplier_id": supplier.id}, self.actor)
        self.assertEqual(revised.state, DraftState.READY)
        self.assertEqual(revised.preparation.preview.brand, material.brand)
        self.assertEqual(revised.preparation.preview.supplier_name, supplier.name)
        self.assertEqual(self.counts()[2:], (0, 0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
