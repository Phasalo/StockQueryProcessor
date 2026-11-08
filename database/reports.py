from contextlib import closing

import psycopg2
from pypika import Table, Case, Order, PostgreSQLQuery, functions, Parameter

from config import WarehouseConfig
from database import TransactionType, FilmType


class WarehouseReports:
    def __init__(self, config: WarehouseConfig):
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
        t = Table("transactions")
        f = Table("films")
        co = Table("consumption")
        c = Table("customers")
        pr_plan = Table("planned_receipts").as_("pr_plan")
        sup_plan = Table("suppliers").as_("sup_plan")
        r = Table("receipts")
        pr_receipt = Table("planned_receipts").as_("pr_receipt")
        sup_receipt = Table("suppliers").as_("sup_receipt")

        q = (
            PostgreSQLQuery.from_(t)
                .join(f).on(f.id == t.film_id)
                .left_join(co).on(co.transaction_id == t.id)
                .left_join(c).on(c.id == co.customer_id)
                .left_join(pr_plan).on(pr_plan.transaction_id == t.id)
                .left_join(sup_plan).on(sup_plan.id == pr_plan.supplier_id)
                .left_join(r).on(r.transaction_id == t.id)
                .left_join(pr_receipt).on(pr_receipt.id == r.planned_receipt_id)
                .left_join(sup_receipt).on(sup_receipt.id == pr_receipt.supplier_id)
                .select(
                t.id,
                t.created_at,
                t.transaction_type,
                f.type,
                f.brand,
                f.format,
                f.thickness,
                t.film_delta_kg,
                c.name.as_("customer_name"),
                c.prod_card_number.as_("customer_prod_card_number"),
                functions.Coalesce(sup_plan.name, sup_receipt.name).as_("supplier_name"),
                functions.Coalesce(pr_plan.planned_date, pr_receipt.planned_date).as_("planned_date"),
                r.actual_date,
            )
                .orderby(t.created_at, order=Order.desc)
                .orderby(t.id, order=Order.desc)
        )

        with closing(psycopg2.connect(self._dsn)) as conn:
            with conn.cursor() as cur:
                cur.execute(str(q))
                rows = cur.fetchall()

        return [
            {
                "id": row[0],
                "created_at": row[1],
                "transaction_type": TransactionType[row[2]],
                "film_type": FilmType[row[3]],
                "film_brand": row[4],
                "film_format": row[5],
                "film_thickness": row[6],
                "film_delta_kg": row[7],
                "customer_name": row[8],
                "customer_prod_card_number": row[9],
                "supplier_name": row[10],
                "planned_date": row[11],
                "actual_date": row[12],
            }
            for row in rows
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
