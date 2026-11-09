from contextlib import closing
from os import getenv
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from config import WarehouseConfig
from database.connection import create_connection
from database.unit_of_work import UnitOfWork


class UnitOfWorkLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.config = WarehouseConfig()

    def test_connection_factory_passes_credentials_as_parameters(self):
        config = WarehouseConfig(password="password@with:/?#symbols")
        with patch("database.connection.psycopg2.connect") as connect:
            self.assertIs(create_connection(config), connect.return_value)
        connect.assert_called_once_with(
            dbname=config.database,
            user=config.user,
            password=config.password,
            host=config.host,
            port=config.port,
        )

    def test_connection_opens_only_inside_context(self):
        with patch("database.unit_of_work.create_connection") as connect:
            uow = UnitOfWork(self.config)
            connect.assert_not_called()
            with uow:
                self.assertIs(uow.connection, connect.return_value)
            connect.assert_called_once_with(self.config)
            connect.return_value.commit.assert_not_called()
            connect.return_value.close.assert_called_once()
        with self.assertRaises(RuntimeError):
            _ = uow.connection

    def test_commit_and_rollback_require_active_context(self):
        uow = UnitOfWork(self.config)
        for operation in (uow.commit, uow.rollback):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(RuntimeError):
                    operation()

    def test_nested_reentry_preserves_outer_connection(self):
        with patch("database.unit_of_work.create_connection") as connect:
            with UnitOfWork(self.config) as uow:
                with self.assertRaises(RuntimeError):
                    with uow:
                        self.fail("Nested reentry must be rejected")
                self.assertIs(uow.connection, connect.return_value)
                connect.assert_called_once_with(self.config)
            connect.return_value.close.assert_called_once()

    def test_rollback_failure_still_closes_connection(self):
        with patch("database.unit_of_work.create_connection") as connect:
            connect.return_value.rollback.side_effect = psycopg2.OperationalError(
                "Connection lost"
            )
            uow = UnitOfWork(self.config)
            with self.assertRaises(psycopg2.OperationalError):
                with uow:
                    pass
            connect.return_value.close.assert_called_once()
        with self.assertRaises(RuntimeError):
            _ = uow.connection

    def test_commit_failure_propagates_and_closes_connection(self):
        with patch("database.unit_of_work.create_connection") as connect:
            connect.return_value.commit.side_effect = psycopg2.OperationalError(
                "Commit failed"
            )
            with self.assertRaises(psycopg2.OperationalError):
                with UnitOfWork(self.config) as uow:
                    uow.commit()
            connect.return_value.rollback.assert_called_once()
            connect.return_value.close.assert_called_once()


class UnitOfWorkPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = getenv("TEST_DATABASE_DSN")
        if not dsn:
            raise unittest.SkipTest("Set TEST_DATABASE_DSN to run PostgreSQL tests")
        settings = parse_dsn(dsn)
        cls.config = WarehouseConfig(
            database=settings["dbname"],
            user=settings["user"],
            password=settings.get("password", ""),
            host=settings["host"],
            port=int(settings.get("port", 5432)),
        )
        cls.schema = "uow_test_" + uuid4().hex
        cls.entries = sql.Identifier(cls.schema, "entries")
        cls.audit = sql.Identifier(cls.schema, "audit_entries")
        with closing(create_connection(cls.config)) as connection, connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema))
                )
                cursor.execute(
                    sql.SQL("CREATE TABLE {} (id int PRIMARY KEY)").format(cls.entries)
                )
                cursor.execute(
                    sql.SQL("CREATE TABLE {} (entry_id int REFERENCES {} (id))").format(
                        cls.audit, cls.entries
                    )
                )
        cls.addClassCleanup(cls.drop_schema)

    @classmethod
    def drop_schema(cls):
        with closing(create_connection(cls.config)) as connection, connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema))
                )

    def setUp(self):
        with closing(create_connection(self.config)) as connection, connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("TRUNCATE {}, {}").format(self.audit, self.entries))

    def insert_pair(self, connection, entry_id=1):
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("INSERT INTO {} (id) VALUES (%s)").format(self.entries),
                (entry_id,),
            )
            cursor.execute(
                sql.SQL("INSERT INTO {} (entry_id) VALUES (%s)").format(self.audit),
                (entry_id,),
            )

    def persisted_counts(self):
        with closing(create_connection(self.config)) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SELECT (SELECT count(*) FROM {}), (SELECT count(*) FROM {})")
                    .format(self.entries, self.audit)
                )
                return cursor.fetchone()

    def test_commit_persists_all_changes(self):
        with UnitOfWork(self.config) as uow:
            connection = uow.connection
            self.insert_pair(connection)
            self.assertEqual(self.persisted_counts(), (0, 0))
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(self.audit))
                self.assertEqual(cursor.fetchone()[0], 1)
            uow.commit()
        self.assertEqual(self.persisted_counts(), (1, 1))
        self.assertTrue(connection.closed)

    def test_context_without_commit_rolls_back_all_changes(self):
        with UnitOfWork(self.config) as uow:
            connection = uow.connection
            self.insert_pair(connection)
        self.assertEqual(self.persisted_counts(), (0, 0))
        self.assertTrue(connection.closed)

    def test_exception_rolls_back_all_changes(self):
        with self.assertRaisesRegex(ValueError, "Stop execution"):
            with UnitOfWork(self.config) as uow:
                connection = uow.connection
                self.insert_pair(connection)
                raise ValueError("Stop execution")
        self.assertEqual(self.persisted_counts(), (0, 0))
        self.assertTrue(connection.closed)

    def test_sql_error_rolls_back_earlier_changes(self):
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            with UnitOfWork(self.config) as uow:
                connection = uow.connection
                self.insert_pair(connection)
                self.insert_pair(connection)
        self.assertEqual(self.persisted_counts(), (0, 0))
        self.assertTrue(connection.closed)

    def test_explicit_rollback_allows_a_new_transaction(self):
        with UnitOfWork(self.config) as uow:
            self.insert_pair(uow.connection)
            uow.rollback()
            self.assertEqual(self.persisted_counts(), (0, 0))
            self.insert_pair(uow.connection, entry_id=2)
            uow.commit()
        self.assertEqual(self.persisted_counts(), (1, 1))
        with closing(create_connection(self.config)) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SELECT id FROM {}").format(self.entries))
                self.assertEqual(cursor.fetchall(), [(2,)])

    def test_changes_after_commit_require_another_commit(self):
        with UnitOfWork(self.config) as uow:
            self.insert_pair(uow.connection)
            uow.commit()
            self.insert_pair(uow.connection, entry_id=2)
        self.assertEqual(self.persisted_counts(), (1, 1))


if __name__ == "__main__":
    unittest.main()
