from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from psycopg2.extensions import connection
from pypika import Parameter, PostgreSQLQuery, Table

from database.enums import TransactionType
from database.repositories.locks import lock_material
from database.repositories.plans import PlanRepository
from database.repositories.transactions import TransactionRepository, validate_quantity


@dataclass(frozen=True)
class ReceiptRecord:
    id: int
    transaction_id: int
    material_id: int
    quantity_delta_kg: Decimal
    supplier_id: int
    planned_receipt_id: int | None
    actual_date: date


@dataclass(frozen=True)
class IssueRecord:
    id: int
    transaction_id: int
    material_id: int
    quantity_delta_kg: Decimal
    customer_id: int
    planned_consumption_id: int | None
    actual_date: date | None


class MovementRepository:
    def __init__(self, connection: connection):
        self._connection = connection
        self._transactions = TransactionRepository(connection)
        self._plans = PlanRepository(connection)

    def add_receipt(
        self, material_id: int, supplier_id: int, actual_date: date,
        quantity_delta_kg: Decimal,
        *, planned_receipt_id: int | None = None,
    ) -> ReceiptRecord:
        validate_quantity(quantity_delta_kg)
        if quantity_delta_kg < 0:
            raise ValueError("Фактический приход должен иметь положительную дельту.")
        self._lock_for_movement(
            TransactionType.RECEIPT, material_id, supplier_id, planned_receipt_id,
        )
        transaction = self._transactions.add(
            TransactionType.RECEIPT, material_id, quantity_delta_kg,
        )
        query = (
            PostgreSQLQuery.into(Table("receipts"))
            .columns("transaction_id", "supplier_id", "actual_date", "planned_receipt_id")
            .insert(*(Parameter("%s") for _ in range(4)))
            .returning("id", "actual_date")
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (transaction.id, supplier_id, actual_date, planned_receipt_id))
            movement_id, saved_date = cursor.fetchone()
        return ReceiptRecord(
            movement_id, transaction.id, material_id, transaction.quantity_delta_kg,
            supplier_id, planned_receipt_id, saved_date,
        )

    def add_issue(
        self, material_id: int, customer_id: int, actual_date: date | None,
        quantity_delta_kg: Decimal,
        *, planned_consumption_id: int | None = None,
    ) -> IssueRecord:
        validate_quantity(quantity_delta_kg)
        if quantity_delta_kg > 0:
            raise ValueError("Фактический расход должен иметь отрицательную дельту.")
        self._lock_for_movement(
            TransactionType.ISSUE, material_id, customer_id, planned_consumption_id,
        )
        transaction = self._transactions.add(
            TransactionType.ISSUE, material_id, quantity_delta_kg,
        )
        query = (
            PostgreSQLQuery.into(Table("issues"))
            .columns("transaction_id", "customer_id", "actual_date", "planned_consumption_id")
            .insert(*(Parameter("%s") for _ in range(4)))
            .returning("id", "actual_date")
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (transaction.id, customer_id, actual_date, planned_consumption_id))
            movement_id, saved_date = cursor.fetchone()
        return IssueRecord(
            movement_id, transaction.id, material_id, transaction.quantity_delta_kg,
            customer_id, planned_consumption_id, saved_date,
        )

    def get_receipt(self, receipt_id: int) -> ReceiptRecord | None:
        receipts = Table("receipts")
        transactions = Table("transactions")
        query = (
            PostgreSQLQuery.from_(receipts)
            .join(transactions).on(transactions.id == receipts.transaction_id)
            .select(
                receipts.id, transactions.id, transactions.material_id,
                transactions.quantity_delta_kg, receipts.supplier_id,
                receipts.planned_receipt_id, receipts.actual_date,
            )
            .where(receipts.id == Parameter("%s"))
            .where(transactions.transaction_type == TransactionType.RECEIPT.name)
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (receipt_id,))
            row = cursor.fetchone()
        return ReceiptRecord(*row) if row else None

    def get_issue(self, issue_id: int) -> IssueRecord | None:
        issues = Table("issues")
        transactions = Table("transactions")
        query = (
            PostgreSQLQuery.from_(issues)
            .join(transactions).on(transactions.id == issues.transaction_id)
            .select(
                issues.id, transactions.id, transactions.material_id,
                transactions.quantity_delta_kg, issues.customer_id,
                issues.planned_consumption_id, issues.actual_date,
            )
            .where(issues.id == Parameter("%s"))
            .where(transactions.transaction_type == TransactionType.ISSUE.name)
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (issue_id,))
            row = cursor.fetchone()
        return IssueRecord(*row) if row else None

    def _lock_for_movement(
        self, kind: TransactionType, material_id: int, party_id: int, plan_id: int | None,
    ) -> None:
        if plan_id is None:
            lock_material(self._connection, material_id)
            return
        plan = (
            self._plans.get_receipt(plan_id) if kind == TransactionType.RECEIPT
            else self._plans.get_consumption(plan_id)
        )
        if plan is None:
            raise ValueError(f"План id={plan_id} для {kind.name} не найден.")
        if plan.material_id != material_id:
            raise ValueError("Материал не совпадает с материалом плана.")
        plan = self._plans.get_for_update(plan.transaction_id)
        if plan.material_id != material_id:
            raise ValueError("Материал не совпадает с материалом плана.")
        expected_party = plan.supplier_id if kind == TransactionType.RECEIPT else plan.customer_id
        if expected_party != party_id:
            raise ValueError("Контрагент не совпадает с контрагентом плана.")
        if plan.is_closed:
            raise ValueError(f"План id={plan_id} уже закрыт.")
