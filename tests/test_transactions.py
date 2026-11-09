import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from decimal import Decimal, localcontext
from os import environ, getenv
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from config import WarehouseConfig
from database.connection import create_connection
from database.enums import TransactionType
from database.repositories.catalog import FilmTypeRepository, MaterialRepository
from database.repositories.transactions import TransactionRecord, TransactionRepository
from database.unit_of_work import UnitOfWork


class TransactionRepositoryPostgresTest(unittest.TestCase):
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
        name = "journal_test_" + uuid4().hex
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
            cls.material_id = MaterialRepository(uow.connection).add(
                film_type.id, "Brand", 1200, 25,
            )

    @classmethod
    def drop_database(cls):
        with closing(create_connection(cls.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.config.database))
                )

    def setUp(self):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("TRUNCATE transactions RESTART IDENTITY CASCADE")

    def transaction_count(self):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM transactions")
                return cursor.fetchone()[0]

    def test_round_trip_returns_decimal_and_timezone_aware_timestamp(self):
        with UnitOfWork(self.config) as uow:
            transactions = TransactionRepository(uow.connection)
            record = transactions.add(TransactionType.RECEIPT, self.material_id, Decimal("12.34"))
            self.assertIsInstance(record, TransactionRecord)
            self.assertEqual(record.material_id, self.material_id)
            self.assertEqual(record.transaction_type, "RECEIPT")
            self.assertIsInstance(record.quantity_delta_kg, Decimal)
            self.assertEqual(record.quantity_delta_kg, Decimal("12.34"))
            self.assertIsNotNone(record.created_at.utcoffset())
            self.assertEqual(transactions.get(record.id), record)
            self.assertIsNone(transactions.get(999999))
        with UnitOfWork(self.config) as uow:
            self.assertEqual(TransactionRepository(uow.connection).get(record.id), record)

    def test_decimal_bounds_and_trailing_zeros_preserve_value(self):
        values = [Decimal("99999999.99"), Decimal("-99999999.99"), Decimal("1.2300")]
        with UnitOfWork(self.config) as uow:
            transactions = TransactionRepository(uow.connection)
            for value in values:
                with self.subTest(value=value), localcontext() as context:
                    context.prec = 2
                    record = transactions.add(TransactionType.ADJUSTMENT, self.material_id, value)
                    self.assertEqual(record.quantity_delta_kg, value)

    def test_all_schema_types_accept_valid_signs(self):
        cases = [
            (TransactionType.CONSUMPTION, "-1.00"),
            (TransactionType.PLANNED_RECEIPT, "1.00"),
            (TransactionType.RECEIPT, "1.00"),
            ("ISSUE", "-1.00"),
            (TransactionType.ADJUSTMENT, "-1.00"),
            (TransactionType.ADJUSTMENT, "1.00"),
        ]
        with UnitOfWork(self.config) as uow:
            transactions = TransactionRepository(uow.connection)
            for kind, value in cases:
                with self.subTest(kind=kind, value=value):
                    record = transactions.add(kind, self.material_id, Decimal(value))
                    self.assertEqual(record.quantity_delta_kg, Decimal(value))
        self.assertEqual(self.transaction_count(), len(cases))

    def test_invalid_quantities_are_rejected_without_rounding(self):
        values = [
            "0", "-0.00", "NaN", "sNaN", "Infinity", "-Infinity",
            "1.001", "-1.001", "0.001", "100000000", "-100000000", "1e1000",
        ]
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    with UnitOfWork(self.config) as uow:
                        TransactionRepository(uow.connection).add(
                            TransactionType.ADJUSTMENT, self.material_id, Decimal(value),
                        )
        self.assertEqual(self.transaction_count(), 0)

    def test_non_decimal_quantities_are_rejected(self):
        for value in (1, 1.23, "1.23", None, True):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    with UnitOfWork(self.config) as uow:
                        TransactionRepository(uow.connection).add(
                            TransactionType.RECEIPT, self.material_id, value,
                        )
        self.assertEqual(self.transaction_count(), 0)

    def test_database_rejects_wrong_signs_and_unknown_types(self):
        cases = [
            ("CONSUMPTION", "1.00", psycopg2.errors.CheckViolation),
            ("ISSUE", "1.00", psycopg2.errors.CheckViolation),
            ("PLANNED_RECEIPT", "-1.00", psycopg2.errors.CheckViolation),
            ("RECEIPT", "-1.00", psycopg2.errors.CheckViolation),
            ("UNKNOWN", "1.00", psycopg2.errors.InvalidTextRepresentation),
        ]
        for kind, value, error in cases:
            with self.subTest(kind=kind):
                with self.assertRaises(error):
                    with UnitOfWork(self.config) as uow:
                        TransactionRepository(uow.connection).add(
                            kind, self.material_id, Decimal(value),
                        )
        self.assertEqual(self.transaction_count(), 0)

    def test_repository_does_not_commit_changes_implicitly(self):
        with UnitOfWork(self.config) as uow:
            transactions = TransactionRepository(uow.connection)
            record = transactions.add(TransactionType.RECEIPT, self.material_id, Decimal("1.00"))
            self.assertIsNotNone(transactions.get(record.id))
            self.assertEqual(self.transaction_count(), 0)
            uow.rollback()
        self.assertEqual(self.transaction_count(), 0)

    def test_foreign_key_error_rolls_back_prior_journal_entries(self):
        with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
            with UnitOfWork(self.config) as uow:
                transactions = TransactionRepository(uow.connection)
                transactions.add(TransactionType.RECEIPT, self.material_id, Decimal("1.00"))
                transactions.add(TransactionType.RECEIPT, 999999, Decimal("1.00"))
        self.assertEqual(self.transaction_count(), 0)


if __name__ == "__main__":
    unittest.main()
