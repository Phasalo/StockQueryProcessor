from decimal import Decimal

from pypika import Case, PostgreSQLQuery, Table, functions
from pypika.terms import NullValue

from semantic_entry.modules.warehouse.enums import TransactionType


def select_plans(kind: TransactionType):
    transactions = Table("transactions")
    cancellations = Table("plan_cancellations")
    cancelled = (
        PostgreSQLQuery.from_(cancellations)
        .select(
            cancellations.plan_transaction_id,
            functions.Sum(cancellations.quantity_kg).as_("quantity_kg"),
        )
        .groupby(cancellations.plan_transaction_id)
    ).as_("cancelled")
    if kind == TransactionType.CONSUMPTION:
        plans = Table("consumption")
        facts = Table("issues")
        plan_link = facts.planned_consumption_id
        customer = plans.customer_id
        supplier = order_date = planned_date = NullValue()
    else:
        plans = Table("planned_receipts")
        facts = Table("receipts")
        plan_link = facts.planned_receipt_id
        supplier = plans.supplier_id
        order_date = plans.order_date
        planned_date = plans.planned_date
        customer = NullValue()
    total = functions.Coalesce(cancelled.quantity_kg, Decimal("0.00"))
    remaining = (
        Case().when(facts.id.notnull(), Decimal("0.00"))
        .else_(functions.Abs(transactions.quantity_delta_kg) - total)
    )
    return (
        PostgreSQLQuery.from_(plans)
        .join(transactions).on(transactions.id == plans.transaction_id)
        .left_join(cancelled).on(cancelled.plan_transaction_id == transactions.id)
        .left_join(facts).on(plan_link == plans.id)
        .select(
            plans.id, transactions.id.as_("transaction_id"), transactions.transaction_type,
            transactions.material_id, transactions.quantity_delta_kg,
            customer.as_("customer_id"), supplier.as_("supplier_id"),
            order_date.as_("order_date"), planned_date.as_("planned_date"),
            total.as_("cancelled_quantity_kg"), remaining.as_("remaining_quantity_kg"),
            facts.id.notnull().as_("is_executed"),
        )
        .where(transactions.transaction_type == kind.name)
    )

