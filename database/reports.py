from contextlib import closing

import psycopg2
from pypika import Table, Case, PostgreSQLQuery, functions, Parameter

from config import WarehouseConfig
from database import TransactionType, FilmType
from database.repositories.reports import ReportRepository
from database.unit_of_work import UnitOfWork


class WarehouseReports:
    def __init__(self, config: WarehouseConfig):
        self._config = config
        self._dsn = f"postgresql://{config.user}:{config.password}@{config.host}:{config.port}/{config.database}"

    def get_film_type_summary(self, film_type: FilmType) -> list[dict]:
        films = Table("films")
        transactions = Table("transactions")

        q = (
            PostgreSQLQuery.from_(films)
                .left_join(transactions)
                .on(transactions.film_id == films.id)
                .select(
                films.format,
                films.thickness,
                functions.Coalesce(
                    functions.Sum(
                        Case()
                            .when(transactions.transaction_type == TransactionType.CONSUMPTION.name,
                                  transactions.film_delta_kg)
                            .else_(0)
                    ),
                    0,
                ).as_("planned_consumption_kg"),
                functions.Coalesce(
                    functions.Sum(
                        Case()
                            .when(transactions.transaction_type == TransactionType.PLANNED_RECEIPT.name,
                                  transactions.film_delta_kg)
                            .else_(0)
                    ),
                    0,
                ).as_("planned_receipt_kg"),
                functions.Coalesce(
                    functions.Sum(
                        Case()
                            .when(transactions.transaction_type == TransactionType.RECEIPT.name,
                                  transactions.film_delta_kg)
                            .when(transactions.transaction_type == TransactionType.ADJUSTMENT.name,
                                  transactions.film_delta_kg)
                            .when(transactions.transaction_type == TransactionType.CONSUMPTION.name,
                                  transactions.film_delta_kg)
                            .else_(0)
                    ),
                    0,
                ).as_("free_balance_kg"),
            )
                .where(films.type == Parameter("%s"))
                .groupby(films.format, films.thickness)
                .orderby(films.format, films.thickness)
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (film_type.name,))
                rows = cur.fetchall()

        return [
            {
                "format": row[0],
                "thickness": row[1],
                "planned_consumption_kg": row[2],
                "planned_receipt_kg": row[3],
                "free_balance_kg": row[4],
            }
            for row in rows
        ]

    def get_film_summary(self, film_type: FilmType, film_format: int, thickness: int) -> dict:
        transactions = Table("transactions")
        films = Table("films")

        q = (
            PostgreSQLQuery.from_(transactions)
                .join(films)
                .on(films.id == transactions.film_id)
                .select(
                functions.Coalesce(
                    functions.Sum(
                        Case()
                            .when(transactions.transaction_type == TransactionType.CONSUMPTION.name,
                                  transactions.film_delta_kg)
                            .else_(0)
                    ),
                    0,
                ).as_("planned_consumption_kg"),
                functions.Coalesce(
                    functions.Sum(
                        Case()
                            .when(transactions.transaction_type == TransactionType.PLANNED_RECEIPT.name,
                                  transactions.film_delta_kg)
                            .else_(0)
                    ),
                    0,
                ).as_("planned_receipt_kg"),
                functions.Coalesce(
                    functions.Sum(
                        Case()
                            .when(transactions.transaction_type == TransactionType.RECEIPT.name,
                                  transactions.film_delta_kg)
                            .when(transactions.transaction_type == TransactionType.ADJUSTMENT.name,
                                  transactions.film_delta_kg)
                            .when(transactions.transaction_type == TransactionType.CONSUMPTION.name,
                                  transactions.film_delta_kg)
                            .else_(0)
                    ),
                    0,
                ).as_("free_balance_kg"),
            )
                .where(films.type == Parameter("%s"))
                .where(films.format == Parameter("%s"))
                .where(films.thickness == Parameter("%s"))
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (film_type.name, film_format, thickness))
                row = cur.fetchone()

        return {
            "planned_consumption_kg": row[0],
            "planned_receipt_kg": row[1],
            "free_balance_kg": row[2],
        }

    def get_planned_consumption_details(self, film_type: FilmType, film_format: int, thickness: int) -> list[dict]:
        transactions = Table("transactions")
        films = Table("films")
        consumption = Table("consumption")
        customers = Table("customers")

        q = (
            PostgreSQLQuery.from_(transactions)
                .join(films)
                .on(films.id == transactions.film_id)
                .join(consumption)
                .on(consumption.transaction_id == transactions.id)
                .join(customers)
                .on(customers.id == consumption.customer_id)
                .select(
                customers.name,
                customers.prod_card_number,
                transactions.film_delta_kg,
                transactions.id,
            )
                .where(transactions.transaction_type == Parameter("%s"))
                .where(films.type == Parameter("%s"))
                .where(films.format == Parameter("%s"))
                .where(films.thickness == Parameter("%s"))
                .orderby(customers.name)
                .orderby(transactions.id)
        )

        params = (
            TransactionType.CONSUMPTION.name,
            film_type.name,
            film_format,
            thickness,
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), params)
                rows = cur.fetchall()

        return [
            {
                "customer_name": row[0],
                "prod_card_number": row[1],
                "delta_kg": row[2],
            }
            for row in rows
        ]

    def get_planned_receipt_details(self, film_type: FilmType, film_format: int, thickness: int) -> list[dict]:
        transactions = Table("transactions")
        films = Table("films")
        planned_receipts = Table("planned_receipts")
        suppliers = Table("suppliers")

        q = (
            PostgreSQLQuery.from_(transactions)
                .join(films)
                .on(films.id == transactions.film_id)
                .join(planned_receipts)
                .on(planned_receipts.transaction_id == transactions.id)
                .join(suppliers)
                .on(suppliers.id == planned_receipts.supplier_id)
                .select(
                suppliers.name,
                planned_receipts.planned_date,
                transactions.film_delta_kg,
                transactions.id,
            )
                .where(transactions.transaction_type == Parameter("%s"))
                .where(films.type == Parameter("%s"))
                .where(films.format == Parameter("%s"))
                .where(films.thickness == Parameter("%s"))
                .orderby(planned_receipts.planned_date)
                .orderby(suppliers.name)
                .orderby(transactions.id)
        )

        params = (
            TransactionType.PLANNED_RECEIPT.name,
            film_type.name,
            film_format,
            thickness,
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), params)
                rows = cur.fetchall()

        return [
            {
                "supplier_name": row[0],
                "planned_date": row[1],
                "delta_kg": row[2],
            }
            for row in rows
        ]

    def get_all_transactions(self) -> list[dict]:
        with UnitOfWork(self._config) as uow:
            records = ReportRepository(uow.connection).list_transactions()
        return [
            {
                "id": row.transaction_id,
                "created_at": row.created_at,
                "transaction_type": TransactionType[row.transaction_type],
                "material_id": row.material_id,
                "film_type": FilmType.__members__.get(row.film_type_code, row.film_type_code),
                "film_type_name": row.film_type_name,
                "film_brand": row.brand,
                "film_format": row.format,
                "film_thickness": row.thickness,
                "film_delta_kg": row.quantity_delta_kg,
                "customer_name": row.customer_name,
                "customer_prod_card_number": row.customer_prod_card_number,
                "supplier_name": row.supplier_name,
                "order_date": row.order_date,
                "planned_date": row.planned_date,
                "actual_date": row.actual_date,
            }
            for row in records
        ]

    def get_available_formats(self, film_type: FilmType) -> list[int]:
        films = Table("films")

        q = (
            PostgreSQLQuery.from_(films)
                .select(films.format)
                .distinct()
                .where(films.type == Parameter("%s"))
                .orderby(films.format)
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (film_type.name,))
                rows = cur.fetchall()

        return [row[0] for row in rows]

    def get_available_thicknesses(self, film_type: FilmType) -> list[int]:
        films = Table("films")

        q = (
            PostgreSQLQuery.from_(films)
                .select(films.thickness)
                .distinct()
                .where(films.type == Parameter("%s"))
                .orderby(films.thickness)
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q), (film_type.name,))
                rows = cur.fetchall()

        return [row[0] for row in rows]
