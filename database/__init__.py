from contextlib import closing
from datetime import date
from decimal import Decimal

import psycopg2
from pypika import Table, Parameter, PostgreSQLQuery

from config import WarehouseConfig
from database.enums import FilmType, TransactionType


def _escape_sql(value: str) -> str:
    return str(value).replace("'", "''")


def _sql_join(values: list[str]) -> str:
    return ", ".join(f"'{_escape_sql(v)}'" for v in values)


class Warehouse:
    def __init__(self, config: WarehouseConfig):
        self._dsn = f"postgresql://{config.user}:{config.password}@{config.host}:{config.port}/{config.database}"
        self._create_tables()

    def _create_tables(self) -> None:
        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    DO $$
                    BEGIN
                        CREATE TYPE {FilmType.get_sql_name()} 
                        AS ENUM ({_sql_join(FilmType.get_values())});
                    EXCEPTION
                        WHEN duplicate_object THEN NULL;
                    END $$;
                """)

                cur.execute(f"""
                    DO $$
                    BEGIN
                        CREATE TYPE {TransactionType.get_sql_name()} 
                        AS ENUM ({_sql_join(TransactionType.get_values())});
                    EXCEPTION
                        WHEN duplicate_object THEN NULL;
                    END $$;
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS films (
                        id SERIAL PRIMARY KEY,
                        type enum_film_type NOT NULL,
                        brand VARCHAR(255) NOT NULL,
                        format INT NOT NULL,
                        thickness INT NOT NULL,
                        UNIQUE (type, brand, format, thickness)
                    );
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS customers (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(255) NOT NULL,
                        prod_card_number VARCHAR(50) NOT NULL,
                        UNIQUE (name, prod_card_number)
                    );
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS suppliers (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(255) NOT NULL UNIQUE
                    );
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS transactions (
                        id SERIAL PRIMARY KEY,
                        created_at TIMESTAMP NOT NULL DEFAULT now(),
                        transaction_type enum_transaction_type NOT NULL,
                        film_id INT NOT NULL REFERENCES films(id),
                        film_delta_kg DECIMAL(10,2) NOT NULL
                    );
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS consumption (
                        id SERIAL PRIMARY KEY,
                        transaction_id INT NOT NULL UNIQUE REFERENCES transactions(id),
                        customer_id INT NOT NULL REFERENCES customers(id)
                    );
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS planned_receipts (
                        id SERIAL PRIMARY KEY,
                        transaction_id INT NOT NULL UNIQUE REFERENCES transactions(id),
                        supplier_id INT NOT NULL REFERENCES suppliers(id),
                        planned_date DATE NOT NULL
                    );
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS receipts (
                        id SERIAL PRIMARY KEY,
                        transaction_id INT NOT NULL UNIQUE REFERENCES transactions(id),
                        planned_receipt_id INT NOT NULL UNIQUE REFERENCES planned_receipts(id),
                        actual_date DATE NOT NULL
                    );
                """)

                cur.execute("""
                    CREATE EXTENSION IF NOT EXISTS pg_trgm;
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_customers_name_trgm
                        ON customers USING gin (name gin_trgm_ops);
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_suppliers_name_trgm
                        ON suppliers USING gin (name gin_trgm_ops);
                """)

    def add_film(self, film_type: FilmType, brand: str, film_format: int, thickness: int) -> int:
        films = Table("films")
        q = (
            PostgreSQLQuery.into(films)
                .columns("type", "brand", "format", "thickness")
                .insert(Parameter("%s"), Parameter("%s"), Parameter("%s"), Parameter("%s"))
                .returning("id")
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (film_type.name, brand, film_format, thickness))
                film_id = cur.fetchone()[0]

        return film_id

    def find_film(self, film_type: FilmType, brand: str, film_format: int, thickness: int) -> int | None:
        films = Table("films")
        q = (
            PostgreSQLQuery.from_(films)
                .select(films.id)
                .where(films.type == Parameter("%s"))
                .where(films.brand == Parameter("%s"))
                .where(films.format == Parameter("%s"))
                .where(films.thickness == Parameter("%s"))
                .limit(1)
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (film_type.name, brand, film_format, thickness))
                row = cur.fetchone()

        return row[0] if row else None

    def search_films(self, film_type=None, brand=None, film_format=None, thickness=None, limit=10) -> list[dict]:
        films = Table("films")
        q = PostgreSQLQuery.from_(films).select(
            films.id, films.type, films.brand, films.format, films.thickness
        )
        params: list = []

        if film_type is not None:
            q = q.where(films.type == Parameter("%s"))
            params.append(film_type.name)
        if brand:
            q = q.where(films.brand.ilike(Parameter("%s")))
            params.append(f"%{brand}%")
        if film_format is not None:
            q = q.where(films.format == Parameter("%s"))
            params.append(film_format)
        if thickness is not None:
            q = q.where(films.thickness == Parameter("%s"))
            params.append(thickness)

        q = q.order_by(films.id).limit(limit)

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), tuple(params))
                rows = cur.fetchall()

        return [
            {"id": r[0], "type": r[1], "brand": r[2], "format": r[3], "thickness": r[4]}
            for r in rows
        ]

    def get_film(self, film_id: int) -> dict | None:
        films = Table("films")
        q = (
            PostgreSQLQuery.from_(films)
                .select(films.id, films.type, films.brand, films.format, films.thickness)
                .where(films.id == Parameter("%s"))
                .limit(1)
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (film_id,))
                r = cur.fetchone()

        if r is None:
            return None
        return {"id": r[0], "type": r[1], "brand": r[2], "format": r[3], "thickness": r[4]}

    def add_customer(self, name: str, prod_card_number: str) -> int:
        customers = Table("customers")
        q = (
            PostgreSQLQuery.into(customers)
                .columns("name", "prod_card_number")
                .insert(Parameter("%s"), Parameter("%s"))
                .returning("id")
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (name, prod_card_number))
                customer_id = cur.fetchone()[0]

        return customer_id

    def find_customer(self, name: str, prod_card_number: str) -> int | None:
        customers = Table("customers")
        q = (
            PostgreSQLQuery.from_(customers)
                .select(customers.id)
                .where(customers.name == Parameter("%s"))
                .where(customers.prod_card_number == Parameter("%s"))
                .limit(1)
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (name, prod_card_number))
                row = cur.fetchone()

        return row[0] if row else None

    def search_customers(self, query: str, limit: int = 10) -> list[dict]:
        customers = Table("customers")
        q = PostgreSQLQuery.from_(customers).select(
            customers.id, customers.name, customers.prod_card_number
        )
        params: list = []
        if query:
            q = q.where(customers.name.ilike(Parameter("%s")))
            params.append(f"%{query}%")
        q = q.order_by(customers.id).limit(limit)

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), tuple(params))
                rows = cur.fetchall()

        return [
            {"id": r[0], "name": r[1], "prod_card_number": r[2]}
            for r in rows
        ]

    def get_customer(self, customer_id: int) -> dict | None:
        customers = Table("customers")
        q = (
            PostgreSQLQuery.from_(customers)
                .select(customers.id, customers.name, customers.prod_card_number)
                .where(customers.id == Parameter("%s"))
                .limit(1)
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (customer_id,))
                r = cur.fetchone()

        if r is None:
            return None
        return {"id": r[0], "name": r[1], "prod_card_number": r[2]}

    def add_supplier(self, name: str) -> int:
        suppliers = Table("suppliers")
        q = (
            PostgreSQLQuery.into(suppliers)
                .columns("name")
                .insert(Parameter("%s"))
                .returning("id")
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (name,))
                supplier_id = cur.fetchone()[0]

        return supplier_id

    def find_supplier(self, name: str) -> int | None:
        suppliers = Table("suppliers")
        q = (
            PostgreSQLQuery.from_(suppliers)
                .select(suppliers.id)
                .where(suppliers.name == Parameter("%s"))
                .limit(1)
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (name,))
                row = cur.fetchone()

        return row[0] if row else None

    def search_suppliers(self, query: str, limit: int = 10) -> list[dict]:
        suppliers = Table("suppliers")
        q = PostgreSQLQuery.from_(suppliers).select(suppliers.id, suppliers.name)
        params: list = []
        if query:
            q = q.where(suppliers.name.ilike(Parameter("%s")))
            params.append(f"%{query}%")
        q = q.order_by(suppliers.id).limit(limit)

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), tuple(params))
                rows = cur.fetchall()

        return [{"id": r[0], "name": r[1]} for r in rows]

    def get_supplier(self, supplier_id: int) -> dict | None:
        suppliers = Table("suppliers")
        q = (
            PostgreSQLQuery.from_(suppliers)
                .select(suppliers.id, suppliers.name)
                .where(suppliers.id == Parameter("%s"))
                .limit(1)
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (supplier_id,))
                r = cur.fetchone()

        if r is None:
            return None
        return {"id": r[0], "name": r[1]}

    @staticmethod
    def _validate_delta(delta: Decimal, tt: TransactionType) -> None:
        info = tt.value
        if delta == 0:
            raise ValueError(
                f"Для типа '{info.ru_name}' delta_kg не может быть равен 0."
            )
        if delta > 0 and not info.can_be_positive:
            raise ValueError(
                f"Для типа '{info.ru_name}' delta_kg не может быть положительным."
            )
        if delta < 0 and not info.can_be_negative:
            raise ValueError(
                f"Для типа '{info.ru_name}' delta_kg не может быть отрицательным."
            )

    def create_consumption(self, film_id: int, customer_id: int, delta_kg: Decimal) -> int:
        tt = TransactionType.CONSUMPTION
        self._validate_delta(delta_kg, tt)

        transactions = Table("transactions")
        consumption = Table("consumption")

        q_tx = (
            PostgreSQLQuery.into(transactions)
                .columns("transaction_type", "film_id", "film_delta_kg")
                .insert(Parameter("%s"), Parameter("%s"), Parameter("%s"))
                .returning("id")
        )
        q_c = (
            PostgreSQLQuery.into(consumption)
                .columns("transaction_id", "customer_id")
                .insert(Parameter("%s"), Parameter("%s"))
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q_tx), (tt.name, film_id, delta_kg))
                tx_id = cur.fetchone()[0]
                cur.execute(str(q_c), (tx_id, customer_id))

        return tx_id

    def create_adjustment(self, film_id: int, delta_kg: Decimal) -> int:
        tt = TransactionType.ADJUSTMENT
        self._validate_delta(delta_kg, tt)

        transactions = Table("transactions")
        q_tx = (
            PostgreSQLQuery.into(transactions)
                .columns("transaction_type", "film_id", "film_delta_kg")
                .insert(Parameter("%s"), Parameter("%s"), Parameter("%s"))
                .returning("id")
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q_tx), (tt.name, film_id, delta_kg))
                tx_id = cur.fetchone()[0]

        return tx_id

    def create_planned_receipt(self, film_id: int, supplier_id: int, planned_date: date, delta_kg: Decimal) -> int:
        tt = TransactionType.PLANNED_RECEIPT
        self._validate_delta(delta_kg, tt)

        transactions = Table("transactions")
        planned_receipts = Table("planned_receipts")

        q_tx = (
            PostgreSQLQuery.into(transactions)
                .columns("transaction_type", "film_id", "film_delta_kg")
                .insert(Parameter("%s"), Parameter("%s"), Parameter("%s"))
                .returning("id")
        )
        q_pr = (
            PostgreSQLQuery.into(planned_receipts)
                .columns("transaction_id", "supplier_id", "planned_date")
                .insert(Parameter("%s"), Parameter("%s"), Parameter("%s"))
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q_tx), (tt.name, film_id, delta_kg))
                tx_id = cur.fetchone()[0]
                cur.execute(str(q_pr), (tx_id, supplier_id, planned_date))

        return tx_id

    def find_open_planned_receipts(self, film_id: int | None = None, supplier_id: int | None = None, limit: int = 10) -> list[dict]:
        planned_receipts = Table("planned_receipts")
        transactions = Table("transactions")
        receipts = Table("receipts")

        q = (
            PostgreSQLQuery.from_(planned_receipts)
                .join(transactions)
                .on(planned_receipts.transaction_id == transactions.id)
                .left_join(receipts)
                .on(receipts.planned_receipt_id == planned_receipts.id)
                .select(
                planned_receipts.id,
                transactions.film_id,
                planned_receipts.supplier_id,
                planned_receipts.planned_date,
                transactions.film_delta_kg,
            )
                .where(receipts.id.isnull())
        )

        params: list = []
        if film_id is not None:
            q = q.where(transactions.film_id == Parameter("%s"))
            params.append(film_id)
        if supplier_id is not None:
            q = q.where(planned_receipts.supplier_id == Parameter("%s"))
            params.append(supplier_id)

        q = q.order_by(planned_receipts.id).limit(limit)

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), tuple(params))
                rows = cur.fetchall()

        return [
            {
                "id": r[0],
                "film_id": r[1],
                "supplier_id": r[2],
                "planned_date": r[3],
                "delta_kg": r[4],
            }
            for r in rows
        ]

    def get_planned_receipt(self, planned_receipt_id: int) -> dict | None:
        planned_receipts = Table("planned_receipts")
        transactions = Table("transactions")
        receipts = Table("receipts")

        q = (
            PostgreSQLQuery.from_(planned_receipts)
                .join(transactions)
                .on(planned_receipts.transaction_id == transactions.id)
                .left_join(receipts)
                .on(receipts.planned_receipt_id == planned_receipts.id)
                .select(
                planned_receipts.id,
                transactions.film_id,
                planned_receipts.supplier_id,
                planned_receipts.planned_date,
                transactions.film_delta_kg,
                receipts.id,
            )
                .where(planned_receipts.id == Parameter("%s"))
                .limit(1)
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (planned_receipt_id,))
                r = cur.fetchone()

        if r is None:
            return None
        return {
            "id": r[0],
            "film_id": r[1],
            "supplier_id": r[2],
            "planned_date": r[3],
            "delta_kg": r[4],
            "is_closed": r[5] is not None,
        }

    def create_receipt(self, film_id: int, planned_receipt_id: int, actual_date: date, delta_kg: Decimal) -> int:
        tt = TransactionType.RECEIPT
        self._validate_delta(delta_kg, tt)

        transactions = Table("transactions")
        planned_receipts = Table("planned_receipts")
        receipts = Table("receipts")

        q_lock = (
            PostgreSQLQuery.from_(planned_receipts)
                .join(transactions)
                .on(planned_receipts.transaction_id == transactions.id)
                .select(transactions.film_id)
                .where(planned_receipts.id == Parameter("%s"))
                .for_update(of=planned_receipts)
        )

        q_check = (
            PostgreSQLQuery.from_(receipts)
                .select(receipts.id)
                .where(receipts.planned_receipt_id == Parameter("%s"))
                .limit(1)
        )
        q_tx = (
            PostgreSQLQuery.into(transactions)
                .columns("transaction_type", "film_id", "film_delta_kg")
                .insert(Parameter("%s"), Parameter("%s"), Parameter("%s"))
                .returning("id")
        )
        q_r = (
            PostgreSQLQuery.into(receipts)
                .columns("transaction_id", "planned_receipt_id", "actual_date")
                .insert(Parameter("%s"), Parameter("%s"), Parameter("%s"))
        )

        with closing(psycopg2.connect(self._dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(str(q_lock), (planned_receipt_id,))
                row = cur.fetchone()
                if row is None:
                    raise ValueError(
                        f"Плановый приход id={planned_receipt_id} не найден."
                    )
                planned_film_id = row[0]

                cur.execute(str(q_check), (planned_receipt_id,))
                existing = cur.fetchone()
                if existing is not None:
                    raise ValueError(
                        f"Плановый приход id={planned_receipt_id} уже закрыт "
                        f"(receipt id={existing[0]})."
                    )

                if planned_film_id != film_id:
                    raise ValueError(
                        f"film_id={film_id} не совпадает с film_id={planned_film_id}, "
                        f"указанным в плановом приходе id={planned_receipt_id}."
                    )

                cur.execute(str(q_tx), (tt.name, planned_film_id, delta_kg))
                tx_id = cur.fetchone()[0]

                try:
                    cur.execute(str(q_r), (tx_id, planned_receipt_id, actual_date))
                except psycopg2.errors.UniqueViolation:
                    raise ValueError(
                        f"Плановый приход id={planned_receipt_id} уже закрыт."
                    )

        return tx_id
