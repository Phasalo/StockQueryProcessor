import subprocess
import sys
import unittest
from contextlib import closing
from dataclasses import replace
from os import environ, getenv
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn

from config import WarehouseConfig
from semantic_entry.modules.warehouse.enums import FilmType
from semantic_entry.modules.warehouse.postgres.connection import create_connection
from semantic_entry.modules.warehouse.postgres.repositories.catalog import (
    CustomerRecord,
    CustomerRepository,
    FilmTypeRecord,
    FilmTypeRepository,
    MaterialRecord,
    MaterialRepository,
    SupplierRecord,
    SupplierRepository,
)
from semantic_entry.modules.warehouse.postgres.unit_of_work import UnitOfWork
from semantic_entry.modules.warehouse.service import Warehouse


class CatalogPostgresTest(unittest.TestCase):
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
        name = "catalog_test_" + uuid4().hex
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
                cursor.execute(
                    "TRUNCATE materials, film_types, customers, suppliers "
                    "RESTART IDENTITY CASCADE"
                )
            types = FilmTypeRepository(uow.connection)
            self.bopp = types.add("BOPP", "Прозрачный BOPP")
            self.cpp = types.add("CPP", "Каст-полипропилен")
        self.warehouse = Warehouse(self.config)

    def test_custom_type_and_material_share_one_transaction(self):
        with UnitOfWork(self.config) as uow:
            types = FilmTypeRepository(uow.connection)
            materials = MaterialRepository(uow.connection)
            custom_type = types.add("CUSTOM", "Новый тип")
            self.assertEqual(types.get(custom_type), FilmTypeRecord(
                custom_type, "CUSTOM", "Новый тип",
            ))
            self.assertEqual(types.find_by_code("CUSTOM"), types.get(custom_type))
            self.assertEqual([record.code for record in types.list()], ["BOPP", "CPP", "CUSTOM"])
            material_id = materials.add(custom_type, "Brand", 1200, 25)
            expected = MaterialRecord(material_id, custom_type, "CUSTOM", "Brand", 1200, 25)
            self.assertEqual(materials.get(material_id), expected)
        with UnitOfWork(self.config) as uow:
            self.assertEqual(MaterialRepository(uow.connection).get(material_id), expected)

    def test_find_uses_all_four_material_characteristics(self):
        variants = [
            (self.bopp, "Brand", 1200, 25),
            (self.cpp, "Brand", 1200, 25),
            (self.bopp, "Other", 1200, 25),
            (self.bopp, "Brand", 1400, 25),
            (self.bopp, "Brand", 1200, 30),
        ]
        with UnitOfWork(self.config) as uow:
            materials = MaterialRepository(uow.connection)
            for values in variants:
                with self.subTest(values=values):
                    material_id = materials.add(*values)
                    self.assertEqual(materials.find(*values).id, material_id)
            self.assertIsNone(materials.find(self.bopp, "Missing", 1200, 25))
            self.assertIsNone(materials.get(999999))
            types = FilmTypeRepository(uow.connection)
            self.assertIsNone(types.get(999999))
            self.assertIsNone(types.find_by_code("MISSING"))

    def test_duplicate_material_rolls_back_type_and_material_together(self):
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            with UnitOfWork(self.config) as uow:
                custom_type = FilmTypeRepository(uow.connection).add("CUSTOM", "Новый тип")
                materials = MaterialRepository(uow.connection)
                materials.add(custom_type, "Brand", 1200, 25)
                materials.add(custom_type, "Brand", 1200, 25)
        with UnitOfWork(self.config) as uow:
            self.assertIsNone(FilmTypeRepository(uow.connection).find_by_code("CUSTOM"))
            self.assertEqual(MaterialRepository(uow.connection).search(), [])

    def test_repositories_do_not_commit_changes_implicitly(self):
        with UnitOfWork(self.config) as uow:
            custom_type = FilmTypeRepository(uow.connection).add("CUSTOM", "Новый тип")
            materials = MaterialRepository(uow.connection)
            material_id = materials.add(custom_type, "Brand", 1200, 25)
            self.assertIsNotNone(materials.get(material_id))
            self.assertEqual(len(materials.search()), 1)
            self.assertIsNone(self.warehouse.find_film("CUSTOM", "Brand", 1200, 25))
            uow.rollback()
        with UnitOfWork(self.config) as uow:
            self.assertIsNone(FilmTypeRepository(uow.connection).find_by_code("CUSTOM"))
            self.assertEqual(MaterialRepository(uow.connection).search(), [])

    def test_material_requires_existing_film_type(self):
        with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
            with UnitOfWork(self.config) as uow:
                MaterialRepository(uow.connection).add(999999, "Brand", 1200, 25)

    def test_material_dimensions_must_be_positive(self):
        for width, thickness in ((0, 25), (-1, 25), (1200, 0), (1200, -1)):
            with self.subTest(width=width, thickness=thickness):
                with self.assertRaises(psycopg2.errors.CheckViolation):
                    with UnitOfWork(self.config) as uow:
                        MaterialRepository(uow.connection).add(
                            self.bopp, "Brand", width, thickness,
                        )

    def test_search_filters_order_and_limit(self):
        with UnitOfWork(self.config) as uow:
            materials = MaterialRepository(uow.connection)
            first = materials.add(self.bopp, "Acme", 1200, 25)
            second = materials.add(self.bopp, "Acme Plus", 1400, 30)
            third = materials.add(self.cpp, "Acme", 1200, 25)
            fourth = materials.add(self.bopp, "Other", 1200, 25)
            self.assertEqual(
                [row.id for row in materials.search()], [first, second, third, fourth],
            )
            self.assertEqual(
                [row.id for row in materials.search(self.bopp, "acm", 1200, 25)], [first],
            )
            self.assertEqual(
                [row.id for row in materials.search(brand="acme")], [first, second, third],
            )
            self.assertEqual(
                [row.id for row in materials.search(film_type_id=self.cpp)], [third],
            )
            self.assertEqual(
                [row.id for row in materials.search(film_format=1400)], [second],
            )
            self.assertEqual(
                [row.id for row in materials.search(thickness=30)], [second],
            )
            self.assertEqual([row.id for row in materials.search(limit=1)], [first])
            self.assertEqual(materials.search(limit=0), [])

    def test_brand_values_are_bound_as_parameters(self):
        brand = "Brand'; DROP TABLE materials; --"
        with UnitOfWork(self.config) as uow:
            materials = MaterialRepository(uow.connection)
            material_id = materials.add(self.bopp, brand, 1200, 25)
            self.assertEqual(materials.find(self.bopp, brand, 1200, 25).id, material_id)
            self.assertEqual([row.id for row in materials.search(brand=brand)], [material_id])
            self.assertEqual(materials.get(material_id).brand, brand)

    def test_facade_preserves_existing_methods_and_result_fields(self):
        material_id = self.warehouse.add_film(FilmType.BOPP, "Brand", 1200, 25)
        expected = {
            "id": material_id, "type": "BOPP", "brand": "Brand",
            "format": 1200, "thickness": 25,
        }
        self.assertEqual(self.warehouse.get_film(material_id), expected)
        self.assertEqual(self.warehouse.find_film(FilmType.BOPP, "Brand", 1200, 25), material_id)
        self.assertEqual(
            self.warehouse.search_films(FilmType.BOPP, "bra", 1200, 25, 1), [expected],
        )
        self.assertEqual(self.warehouse.search_films(), [expected])
        self.assertIsNone(self.warehouse.get_film(999999))
        self.assertIsNone(self.warehouse.find_film(FilmType.BOPP, "Missing", 1200, 25))

    def test_facade_accepts_film_type_codes_from_database(self):
        with UnitOfWork(self.config) as uow:
            FilmTypeRepository(uow.connection).add("CUSTOM", "Новый тип")
        material_id = self.warehouse.add_film("CUSTOM", "Brand", 1200, 25)
        self.assertEqual(self.warehouse.get_film(material_id)["type"], "CUSTOM")
        self.assertEqual(self.warehouse.find_film("CUSTOM", "Brand", 1200, 25), material_id)
        self.assertEqual(len(self.warehouse.search_films("CUSTOM")), 1)

    def test_unknown_type_does_not_create_or_match_other_materials(self):
        self.warehouse.add_film(FilmType.BOPP, "Brand", 1200, 25)
        self.assertEqual(self.warehouse.search_films("MISSING"), [])
        self.assertIsNone(self.warehouse.find_film("MISSING", "Brand", 1200, 25))
        with self.assertRaisesRegex(ValueError, "MISSING"):
            self.warehouse.add_film("MISSING", "Brand", 1200, 25)
        self.assertEqual(len(self.warehouse.search_films()), 1)

    def test_customer_record_preserves_card_number_as_text(self):
        with UnitOfWork(self.config) as uow:
            customers = CustomerRepository(uow.connection)
            customer_id = customers.add("Заказчик", "000123")
            expected = CustomerRecord(customer_id, "Заказчик", "000123")
            self.assertEqual(customers.get(customer_id), expected)
            self.assertEqual(customers.find("Заказчик", "000123"), expected)
            self.assertIsNone(customers.find("Заказчик", "123"))
            self.assertIsNone(customers.get(999999))
            self.assertIsNone(customers.find("Missing", "000123"))
        with UnitOfWork(self.config) as uow:
            self.assertEqual(CustomerRepository(uow.connection).get(customer_id), expected)

    def test_customer_uniqueness_uses_name_and_card(self):
        first = self.warehouse.add_customer("Company", "001")
        second = self.warehouse.add_customer("Company", "002")
        self.assertNotEqual(first, second)
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            with UnitOfWork(self.config) as uow:
                CustomerRepository(uow.connection).add("Company", "001")
        self.assertEqual(len(self.warehouse.search_customers("Company")), 2)

    def test_supplier_records_and_missing_values(self):
        with UnitOfWork(self.config) as uow:
            suppliers = SupplierRepository(uow.connection)
            supplier_id = suppliers.add("Поставщик")
            expected = SupplierRecord(supplier_id, "Поставщик")
            self.assertEqual(suppliers.get(supplier_id), expected)
            self.assertEqual(suppliers.find("Поставщик"), expected)
            self.assertIsNone(suppliers.get(999999))
            self.assertIsNone(suppliers.find("Missing"))
        with UnitOfWork(self.config) as uow:
            self.assertEqual(SupplierRepository(uow.connection).get(supplier_id), expected)

    def test_customer_and_supplier_share_one_transaction(self):
        with UnitOfWork(self.config) as uow:
            customers = CustomerRepository(uow.connection)
            suppliers = SupplierRepository(uow.connection)
            customer_id = customers.add("Customer", "001")
            supplier_id = suppliers.add("Supplier")
            self.assertIsNotNone(customers.get(customer_id))
            self.assertIsNotNone(suppliers.get(supplier_id))
            with UnitOfWork(self.config) as other:
                self.assertEqual(CustomerRepository(other.connection).search(""), [])
                self.assertEqual(SupplierRepository(other.connection).search(""), [])
        self.assertIsNotNone(self.warehouse.get_customer(customer_id))
        self.assertIsNotNone(self.warehouse.get_supplier(supplier_id))

    def test_party_repositories_do_not_commit_implicitly(self):
        with UnitOfWork(self.config) as uow:
            customers = CustomerRepository(uow.connection)
            suppliers = SupplierRepository(uow.connection)
            customers.add("Customer", "001")
            suppliers.add("Supplier")
            self.assertEqual(len(customers.search("")), 1)
            self.assertEqual(len(suppliers.search("")), 1)
            self.assertEqual(self.warehouse.search_customers(""), [])
            self.assertEqual(self.warehouse.search_suppliers(""), [])
            uow.rollback()
        self.assertEqual(self.warehouse.search_customers(""), [])
        self.assertEqual(self.warehouse.search_suppliers(""), [])

    def test_duplicate_supplier_rolls_back_customer_in_same_transaction(self):
        supplier_id = self.warehouse.add_supplier("Supplier")
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            with UnitOfWork(self.config) as uow:
                CustomerRepository(uow.connection).add("Customer", "001")
                SupplierRepository(uow.connection).add("Supplier")
        self.assertIsNone(self.warehouse.find_customer("Customer", "001"))
        self.assertEqual(self.warehouse.find_supplier("Supplier"), supplier_id)
        self.assertEqual(len(self.warehouse.search_suppliers("")), 1)

    def test_party_search_filters_order_and_limits(self):
        with UnitOfWork(self.config) as uow:
            customers = CustomerRepository(uow.connection)
            suppliers = SupplierRepository(uow.connection)
            customer_ids = [
                customers.add("B Acme", "001"),
                customers.add("A Acme", "002"),
                customers.add("Other", "003"),
            ]
            supplier_ids = [
                suppliers.add("B Acme"), suppliers.add("A Acme"), suppliers.add("Other"),
            ]
            for repository, ids in ((customers, customer_ids), (suppliers, supplier_ids)):
                with self.subTest(repository=type(repository).__name__):
                    self.assertEqual([row.id for row in repository.search("")], ids)
                    self.assertEqual([row.id for row in repository.search("acm")], ids[:2])
                    self.assertEqual([row.id for row in repository.search("", 1)], ids[:1])
                    self.assertEqual(repository.search("", 0), [])
                    self.assertEqual(repository.search("Missing"), [])

    def test_party_names_are_bound_as_parameters(self):
        name = "Company'; DROP TABLE suppliers; --"
        with UnitOfWork(self.config) as uow:
            customers = CustomerRepository(uow.connection)
            suppliers = SupplierRepository(uow.connection)
            customer_id = customers.add(name, "001")
            supplier_id = suppliers.add(name)
            self.assertEqual(customers.find(name, "001").id, customer_id)
            self.assertEqual(suppliers.find(name).id, supplier_id)
            self.assertEqual([row.id for row in customers.search(name)], [customer_id])
            self.assertEqual([row.id for row in suppliers.search(name)], [supplier_id])
        self.assertEqual(self.warehouse.get_customer(customer_id)["name"], name)
        self.assertEqual(self.warehouse.get_supplier(supplier_id)["name"], name)

    def test_customer_facade_preserves_methods_and_result_fields(self):
        customer_id = self.warehouse.add_customer("Customer", "000007")
        expected = {"id": customer_id, "name": "Customer", "prod_card_number": "000007"}
        self.assertEqual(self.warehouse.get_customer(customer_id), expected)
        self.assertEqual(self.warehouse.find_customer("Customer", "000007"), customer_id)
        self.assertEqual(self.warehouse.search_customers(query="cust", limit=1), [expected])
        self.assertIsNone(self.warehouse.get_customer(999999))
        self.assertIsNone(self.warehouse.find_customer("Missing", "000007"))

    def test_supplier_facade_preserves_methods_and_result_fields(self):
        supplier_id = self.warehouse.add_supplier("Supplier")
        expected = {"id": supplier_id, "name": "Supplier"}
        self.assertEqual(self.warehouse.get_supplier(supplier_id), expected)
        self.assertEqual(self.warehouse.find_supplier("Supplier"), supplier_id)
        self.assertEqual(self.warehouse.search_suppliers(query="supp", limit=1), [expected])
        self.assertIsNone(self.warehouse.get_supplier(999999))
        self.assertIsNone(self.warehouse.find_supplier("Missing"))


if __name__ == "__main__":
    unittest.main()
