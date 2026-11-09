from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from psycopg2.extensions import connection
from pypika import Order, PostgreSQLQuery, Table, functions

from database.enums import TransactionType


@dataclass(frozen=True)
class JournalRecord:
    transaction_id: int
    created_at: datetime
    transaction_type: str
    material_id: int
    film_type_code: str
    film_type_name: str
    brand: str
    format: int
    thickness: int
    quantity_delta_kg: Decimal
    customer_id: int | None
    customer_name: str | None
    customer_prod_card_number: str | None
    supplier_id: int | None
    supplier_name: str | None
    order_date: date | None
    planned_date: date | None
    actual_date: date | None


class ReportRepository:
    def __init__(self, connection: connection):
        self._connection = connection

    def list_transactions(self) -> list[JournalRecord]:
        transactions = Table("transactions")
        materials = Table("materials")
        film_types = Table("film_types")
        consumption = Table("consumption")
        issues = Table("issues")
        receipts = Table("receipts")
        planned_receipts = Table("planned_receipts")
        receipt_plan = Table("planned_receipts").as_("receipt_plan")
        consumption_customer = Table("customers").as_("consumption_customer")
        issue_customer = Table("customers").as_("issue_customer")
        planned_supplier = Table("suppliers").as_("planned_supplier")
        receipt_supplier = Table("suppliers").as_("receipt_supplier")
        query = (
            PostgreSQLQuery.from_(transactions)
            .join(materials).on(materials.id == transactions.material_id)
            .join(film_types).on(film_types.id == materials.film_type_id)
            .left_join(consumption).on(
                (consumption.transaction_id == transactions.id)
                & (transactions.transaction_type == TransactionType.CONSUMPTION.name)
            )
            .left_join(consumption_customer).on(consumption_customer.id == consumption.customer_id)
            .left_join(issues).on(
                (issues.transaction_id == transactions.id)
                & (transactions.transaction_type == TransactionType.ISSUE.name)
            )
            .left_join(issue_customer).on(issue_customer.id == issues.customer_id)
            .left_join(planned_receipts).on(
                (planned_receipts.transaction_id == transactions.id)
                & (transactions.transaction_type == TransactionType.PLANNED_RECEIPT.name)
            )
            .left_join(planned_supplier).on(planned_supplier.id == planned_receipts.supplier_id)
            .left_join(receipts).on(
                (receipts.transaction_id == transactions.id)
                & (transactions.transaction_type == TransactionType.RECEIPT.name)
            )
            .left_join(receipt_supplier).on(receipt_supplier.id == receipts.supplier_id)
            .left_join(receipt_plan).on(receipt_plan.id == receipts.planned_receipt_id)
            .select(
                transactions.id, transactions.created_at, transactions.transaction_type,
                materials.id, film_types.code, film_types.name,
                materials.brand, materials.format, materials.thickness,
                transactions.quantity_delta_kg,
                functions.Coalesce(consumption.customer_id, issues.customer_id),
                functions.Coalesce(consumption_customer.name, issue_customer.name),
                functions.Coalesce(consumption_customer.prod_card_number, issue_customer.prod_card_number),
                functions.Coalesce(planned_receipts.supplier_id, receipts.supplier_id),
                functions.Coalesce(planned_supplier.name, receipt_supplier.name),
                functions.Coalesce(planned_receipts.order_date, receipt_plan.order_date),
                functions.Coalesce(planned_receipts.planned_date, receipt_plan.planned_date),
                functions.Coalesce(receipts.actual_date, issues.actual_date),
            )
            .orderby(transactions.created_at, order=Order.desc)
            .orderby(transactions.id, order=Order.desc)
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query))
            return [JournalRecord(*row) for row in cursor.fetchall()]
