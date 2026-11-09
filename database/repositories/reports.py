from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from psycopg2.extensions import connection
from pypika import Order, Parameter, PostgreSQLQuery, Table, functions

from database.enums import TransactionType
from database.repositories.plan_queries import select_plans


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


@dataclass(frozen=True)
class StockRecord:
    material_id: int
    film_type_code: str
    film_type_name: str
    brand: str
    format: int
    thickness: int
    on_hand_kg: Decimal
    reserved_kg: Decimal
    planned_receipt_kg: Decimal
    available_kg: Decimal
    projected_kg: Decimal


@dataclass(frozen=True)
class ConsumptionDetailRecord:
    plan_id: int
    transaction_id: int
    material_id: int
    customer_id: int
    customer_name: str
    prod_card_number: str
    remaining_quantity_kg: Decimal


@dataclass(frozen=True)
class ReceiptPlanDetailRecord:
    plan_id: int
    transaction_id: int
    material_id: int
    supplier_id: int
    supplier_name: str
    order_date: date
    planned_date: date
    remaining_quantity_kg: Decimal


class ReportRepository:
    def __init__(self, connection: connection):
        self._connection = connection

    def get_stock(self, material_id: int) -> StockRecord | None:
        query = self._stock_query().where(Table("materials").id == Parameter("%s"))
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (material_id,))
            row = cursor.fetchone()
        return StockRecord(*row) if row else None

    def list_stock(
        self, film_type_code: str | None = None, *, film_format: int | None = None,
        thickness: int | None = None, brand: str | None = None,
    ) -> list[StockRecord]:
        query = self._stock_query()
        params = []
        if film_type_code is not None:
            query = query.where(Table("film_types").code == Parameter("%s"))
            params.append(film_type_code)
        materials = Table("materials")
        for field, value in (("format", film_format), ("thickness", thickness), ("brand", brand)):
            if value is not None:
                query = query.where(materials[field] == Parameter("%s"))
                params.append(value)
        query = query.orderby(
            Table("film_types").code, materials.format, materials.thickness,
            materials.brand, materials.id,
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), tuple(params))
            return [StockRecord(*row) for row in cursor.fetchall()]

    def list_open_consumption(self, material_id: int) -> list[ConsumptionDetailRecord]:
        plans = select_plans(TransactionType.CONSUMPTION).as_("plans")
        customers = Table("customers")
        query = (
            PostgreSQLQuery.from_(plans).join(customers).on(customers.id == plans.customer_id)
            .select(
                plans.id, plans.transaction_id, plans.material_id, customers.id,
                customers.name, customers.prod_card_number, plans.remaining_quantity_kg,
            )
            .where(plans.material_id == Parameter("%s"))
            .where(plans.remaining_quantity_kg > 0)
            .orderby(customers.name, plans.transaction_id)
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (material_id,))
            return [ConsumptionDetailRecord(*row) for row in cursor.fetchall()]

    def list_open_receipts(self, material_id: int) -> list[ReceiptPlanDetailRecord]:
        plans = select_plans(TransactionType.PLANNED_RECEIPT).as_("plans")
        suppliers = Table("suppliers")
        query = (
            PostgreSQLQuery.from_(plans).join(suppliers).on(suppliers.id == plans.supplier_id)
            .select(
                plans.id, plans.transaction_id, plans.material_id, suppliers.id,
                suppliers.name, plans.order_date, plans.planned_date, plans.remaining_quantity_kg,
            )
            .where(plans.material_id == Parameter("%s"))
            .where(plans.remaining_quantity_kg > 0)
            .orderby(plans.planned_date, suppliers.name, plans.transaction_id)
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (material_id,))
            return [ReceiptPlanDetailRecord(*row) for row in cursor.fetchall()]

    def list_formats(self, film_type_code: str) -> list[int]:
        return self._material_values("format", film_type_code)

    def list_thicknesses(self, film_type_code: str) -> list[int]:
        return self._material_values("thickness", film_type_code)

    def list_brands(self, film_type_code: str, film_format: int, thickness: int) -> list[str]:
        return self._material_values("brand", film_type_code, film_format, thickness)

    def _material_values(self, field, film_type_code, film_format=None, thickness=None):
        materials = Table("materials")
        film_types = Table("film_types")
        query = (
            PostgreSQLQuery.from_(materials).join(film_types).on(film_types.id == materials.film_type_id)
            .select(materials[field]).distinct()
            .where(film_types.code == Parameter("%s"))
            .orderby(materials[field])
        )
        params = [film_type_code]
        for name, value in (("format", film_format), ("thickness", thickness)):
            if value is not None:
                query = query.where(materials[name] == Parameter("%s"))
                params.append(value)
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), tuple(params))
            return [row[0] for row in cursor.fetchall()]

    @staticmethod
    def _stock_query():
        materials = Table("materials")
        film_types = Table("film_types")
        transactions = Table("transactions")
        physical = (
            PostgreSQLQuery.from_(transactions)
            .select(transactions.material_id, functions.Sum(transactions.quantity_delta_kg).as_("quantity_kg"))
            .where(transactions.transaction_type.isin([
                TransactionType.RECEIPT.name, TransactionType.ISSUE.name,
                TransactionType.ADJUSTMENT.name,
            ]))
            .groupby(transactions.material_id)
        ).as_("physical")
        reserved = ReportRepository._open_plan_totals(TransactionType.CONSUMPTION).as_("reserved")
        expected = ReportRepository._open_plan_totals(TransactionType.PLANNED_RECEIPT).as_("expected")
        on_hand_kg = functions.Coalesce(physical.quantity_kg, Decimal("0.00"))
        reserved_kg = functions.Coalesce(reserved.quantity_kg, Decimal("0.00"))
        expected_kg = functions.Coalesce(expected.quantity_kg, Decimal("0.00"))
        return (
            PostgreSQLQuery.from_(materials)
            .join(film_types).on(film_types.id == materials.film_type_id)
            .left_join(physical).on(physical.material_id == materials.id)
            .left_join(reserved).on(reserved.material_id == materials.id)
            .left_join(expected).on(expected.material_id == materials.id)
            .select(
                materials.id, film_types.code, film_types.name,
                materials.brand, materials.format, materials.thickness,
                on_hand_kg, reserved_kg, expected_kg,
                on_hand_kg - reserved_kg, on_hand_kg - reserved_kg + expected_kg,
            )
        )

    @staticmethod
    def _open_plan_totals(kind: TransactionType):
        plans = select_plans(kind).as_("open_plans")
        return (
            PostgreSQLQuery.from_(plans)
            .select(plans.material_id, functions.Sum(plans.remaining_quantity_kg).as_("quantity_kg"))
            .where(plans.remaining_quantity_kg > 0)
            .groupby(plans.material_id)
        )

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
