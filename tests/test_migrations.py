import json
import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date
from os import environ, getenv
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from config import WarehouseConfig
from semantic_entry.modules.warehouse.postgres.connection import create_connection
from semantic_entry.modules.warehouse.postgres.unit_of_work import UnitOfWork

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = ROOT / "migrations" / "V001__initial_warehouse.sql"


class InitialMigrationPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = getenv("TEST_DATABASE_DSN")
        if not dsn:
            raise unittest.SkipTest("Set TEST_DATABASE_DSN to run PostgreSQL tests")
        settings = parse_dsn(dsn)
        cls.admin_config = WarehouseConfig(
            database=settings["dbname"],
            user=settings["user"],
            password=settings.get("password", ""),
            host=settings["host"],
            port=int(settings.get("port", 5432)),
        )

    def setUp(self):
        name = "migration_test_" + uuid4().hex
        self.config = replace(self.admin_config, database=name)
        with closing(create_connection(self.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.addCleanup(self.drop_database)

    def drop_database(self):
        with closing(create_connection(self.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(self.config.database))
                )

    def query(self, query, params=()):
        with closing(create_connection(self.config)) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()

    def tables(self):
        return {
            row[0] for row in self.query(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }

    def run_pgmigrate(
        self, command="migrate", *, base_dir=ROOT, dryrun=False, expect_success=True,
    ):
        env = environ.copy()
        env.update(
            PGDATABASE=self.config.database,
            PGUSER=self.config.user,
            PGPASSWORD=self.config.password,
            PGHOST=self.config.host,
            PGPORT=str(self.config.port),
        )
        args = [
            sys.executable, "-m", "pgmigrate", "-d", str(base_dir), "-t", "latest",
            "--check_serial_versions",
        ]
        if dryrun:
            args.append("-n")
        result = subprocess.run(
            [*args, command], env=env, capture_output=True, text=True, timeout=30,
        )
        if expect_success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def copy_migration_project(self, folder):
        base_dir = Path(folder)
        migrations = base_dir / "migrations"
        migrations.mkdir()
        (base_dir / "migrations.yml").write_bytes((ROOT / "migrations.yml").read_bytes())
        (migrations / MIGRATION_PATH.name).write_bytes(MIGRATION_PATH.read_bytes())
        return base_dir

    def add_material(self):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO materials (film_type_id, brand, format, thickness) "
                    "SELECT id, 'Test brand', 1200, 25 FROM film_types "
                    "WHERE code = 'BOPP' RETURNING id"
                )
                material_id = cursor.fetchone()[0]
        return material_id

    def test_install_creates_schema_and_initial_film_types(self):
        info = json.loads(self.run_pgmigrate("info").stdout)
        self.assertIsNone(info["1"]["installed_on"])
        self.assertEqual(self.tables(), set())
        self.run_pgmigrate()
        self.assertEqual(self.tables(), {
            "film_types", "materials", "customers", "suppliers", "transactions",
            "consumption", "issues", "planned_receipts", "receipts",
            "plan_cancellations", "schema_version",
        })
        self.assertEqual(self.query("SELECT code, name FROM film_types ORDER BY code"), [
            ("BOPP", "Прозрачный BOPP"),
            ("BOPP_METAL", "BOPP металлизированный"),
            ("BOPP_PEARL", "BOPP жемчужный (перламутровый)"),
            ("CPP", "Каст-полипропилен"),
            ("LDPE_FLAT_SHEET", "ПВД полотно"),
            ("LDPE_HALF_SLEEVE", "ПВД полурукав"),
        ])
        self.assertEqual(self.query(
            "SELECT version, description, type::text FROM schema_version ORDER BY version"
        ), [
            (1, "initial warehouse", "auto"),
            (2, "engine executions", "auto"),
        ])
        self.assertEqual(self.query("SELECT tablename FROM pg_tables WHERE schemaname = 'engine' ORDER BY tablename"), [
            ("audit_events",), ("executions",), ("idempotency_records",),
        ])
        self.assertEqual(self.query(
            "SELECT numeric_precision, numeric_scale FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'transactions' "
            "AND column_name = 'quantity_delta_kg'"
        ), [(10, 2)])

    def test_nonempty_schema_is_rejected_without_changes(self):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("CREATE TABLE films (id int PRIMARY KEY)")
                cursor.execute("INSERT INTO films VALUES (42)")
        result = self.run_pgmigrate(expect_success=False)
        self.assertIn("empty schema", result.stderr)
        self.assertEqual(self.tables(), {"films"})
        self.assertEqual(self.query("SELECT id FROM films"), [(42,)])

    def test_repeat_install_preserves_existing_data(self):
        self.run_pgmigrate()
        installed_on = self.query("SELECT installed_on FROM schema_version")
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("UPDATE film_types SET name = 'Custom name' WHERE code = 'BOPP'")
        self.run_pgmigrate()
        self.assertEqual(self.query("SELECT name FROM film_types WHERE code = 'BOPP'"), [
            ("Custom name",),
        ])
        self.assertEqual(self.query("SELECT count(*) FROM schema_version"), [(2,)])
        self.assertEqual(self.query("SELECT installed_on FROM schema_version"), installed_on)

    def test_engine_migration_upgrades_warehouse_without_changing_materials(self):
        with TemporaryDirectory() as folder:
            self.run_pgmigrate(base_dir=self.copy_migration_project(folder))
        material_id = self.add_material()
        before = self.query("SELECT * FROM materials ORDER BY id")
        self.run_pgmigrate()
        self.assertEqual(self.query("SELECT * FROM materials ORDER BY id"), before)
        self.assertEqual(before[0][0], material_id)
        self.assertEqual(self.query("SELECT count(*) FROM engine.executions"), [(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM pg_tables WHERE tablename = 'drafts'"), [(0,)])

    def test_engine_migration_error_rolls_back_engine_schema_and_preserves_warehouse(self):
        with TemporaryDirectory() as folder:
            base_dir = self.copy_migration_project(folder)
            self.run_pgmigrate(base_dir=base_dir)
            material_id = self.add_material()
            migration = ROOT / "migrations" / "V002__engine_executions.sql"
            (base_dir / "migrations" / migration.name).write_text(migration.read_text() + "\nSELECT 1 / 0;\n")
            self.run_pgmigrate(base_dir=base_dir, expect_success=False)
        self.assertEqual(self.query("SELECT id FROM materials"), [(material_id,)])
        self.assertEqual(self.query("SELECT count(*) FROM pg_namespace WHERE nspname = 'engine'"), [(0,)])
        self.assertEqual(self.query("SELECT version FROM schema_version"), [(1,)])

    def test_sql_error_rolls_back_schema_seed_and_version(self):
        with TemporaryDirectory() as folder:
            base_dir = self.copy_migration_project(folder)
            (base_dir / "migrations" / MIGRATION_PATH.name).write_text(
                MIGRATION_PATH.read_text(encoding="utf-8") + "\nSELECT 1 / 0;\n",
                encoding="utf-8",
            )
            result = self.run_pgmigrate(base_dir=base_dir, expect_success=False)
            self.assertIn("division by zero", result.stderr)
        self.assertEqual(self.tables(), set())
        self.assertEqual(self.query(
            "SELECT typname FROM pg_type "
            "WHERE typnamespace = 'public'::regnamespace"
        ), [])
        self.run_pgmigrate()
        self.assertEqual(self.query("SELECT count(*) FROM film_types"), [(6,)])

    def test_dryrun_leaves_no_schema_data_or_version(self):
        self.run_pgmigrate(dryrun=True)
        self.assertEqual(self.tables(), set())
        self.assertEqual(self.query(
            "SELECT typname FROM pg_type "
            "WHERE typnamespace = 'public'::regnamespace"
        ), [])

    def test_version_gaps_are_rejected(self):
        with TemporaryDirectory() as folder:
            base_dir = self.copy_migration_project(folder)
            (base_dir / "migrations" / "V003__gap.sql").write_text(
                "SELECT 1;\n", encoding="utf-8",
            )
            self.run_pgmigrate(base_dir=base_dir, expect_success=False)
        self.assertEqual(self.tables(), set())

    def test_transaction_quantity_signs_are_enforced(self):
        self.run_pgmigrate()
        material_id = self.add_material()
        invalid = [
            ("CONSUMPTION", 1), ("ISSUE", 1),
            ("PLANNED_RECEIPT", -1), ("RECEIPT", -1), ("ADJUSTMENT", 0),
        ]
        for transaction_type, quantity in invalid:
            with self.subTest(transaction_type=transaction_type):
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    with UnitOfWork(self.config) as uow:
                        with uow.connection.cursor() as cursor:
                            cursor.execute(
                                "INSERT INTO transactions "
                                "(transaction_type, material_id, quantity_delta_kg) "
                                "VALUES (%s, %s, %s)",
                                (transaction_type, material_id, quantity),
                            )
        self.assertEqual(self.query("SELECT count(*) FROM transactions"), [(0,)])

    def test_plan_links_allow_only_one_actual_movement(self):
        self.run_pgmigrate()
        material_id = self.add_material()
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO customers (name, prod_card_number) "
                    "VALUES ('Customer', '001') RETURNING id"
                )
                customer_id = cursor.fetchone()[0]
                cursor.execute("INSERT INTO suppliers (name) VALUES ('Supplier') RETURNING id")
                supplier_id = cursor.fetchone()[0]
        cases = [
            (
                "CONSUMPTION", "ISSUE", -10, "consumption", "issues",
                "customer_id", customer_id, "planned_consumption_id",
            ),
            (
                "PLANNED_RECEIPT", "RECEIPT", 10, "planned_receipts", "receipts",
                "supplier_id", supplier_id, "planned_receipt_id",
            ),
        ]
        for (
            planned_type, actual_type, quantity, plans, actuals,
            party_field, party_id, plan_field,
        ) in cases:
            with self.subTest(actual_type=actual_type):
                with UnitOfWork(self.config) as uow:
                    with uow.connection.cursor() as cursor:
                        cursor.execute(
                            "INSERT INTO transactions "
                            "(transaction_type, material_id, quantity_delta_kg) "
                            "VALUES (%s, %s, %s) RETURNING id",
                            (planned_type, material_id, quantity),
                        )
                        transaction_id = cursor.fetchone()[0]
                        columns = ["transaction_id", party_field]
                        values = [transaction_id, party_id]
                        if planned_type == "PLANNED_RECEIPT":
                            columns += ["order_date", "planned_date"]
                            values += [date(2026, 11, 1), date(2026, 11, 10)]
                        cursor.execute(
                            sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
                                sql.Identifier(plans),
                                sql.SQL(", ").join(map(sql.Identifier, columns)),
                                sql.SQL(", ").join(sql.Placeholder() for _ in values),
                            ),
                            values,
                        )
                        plan_id = cursor.fetchone()[0]
                for link in (plan_id, None, None):
                    self.add_actual(
                        actual_type, material_id, quantity, actuals,
                        party_field, party_id, plan_field, link,
                    )
                with self.assertRaises(psycopg2.errors.UniqueViolation):
                    self.add_actual(
                        actual_type, material_id, quantity, actuals,
                        party_field, party_id, plan_field, plan_id,
                    )

    def add_actual(
        self, actual_type, material_id, quantity, table,
        party_field, party_id, plan_field, plan_id,
    ):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO transactions (transaction_type, material_id, quantity_delta_kg) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (actual_type, material_id, quantity),
                )
                transaction_id = cursor.fetchone()[0]
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {} (transaction_id, {}, {}, actual_date) "
                        "VALUES (%s, %s, %s, %s)"
                    ).format(
                        sql.Identifier(table), sql.Identifier(party_field),
                        sql.Identifier(plan_field),
                    ),
                    (transaction_id, party_id, plan_id, date(2026, 11, 10)),
                )


if __name__ == "__main__":
    unittest.main()
