from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from psycopg2.extensions import connection
from pypika import Case, Parameter, PostgreSQLQuery, Table, functions
from pypika.terms import NullValue

from database.enums import TransactionType
from database.repositories.transactions import TransactionRepository, validate_quantity


@dataclass(frozen=True)
class PlanRecord:
    id: int
    transaction_id: int
    transaction_type: str
    material_id: int
    quantity_delta_kg: Decimal
    customer_id: int | None
    supplier_id: int | None
    order_date: date | None
    planned_date: date | None
    cancelled_quantity_kg: Decimal
    remaining_quantity_kg: Decimal
    is_executed: bool

    @property
    def is_closed(self) -> bool:
        return self.is_executed or self.remaining_quantity_kg == 0


@dataclass(frozen=True)
class PlanCancellationRecord:
    id: int
    plan_transaction_id: int
    quantity_kg: Decimal
    reason: str
    created_at: datetime


class PlanRepository:
    def __init__(self, connection: connection):
        self._connection = connection
        self._transactions = TransactionRepository(connection)

    def add_consumption(
        self, material_id: int, customer_id: int, quantity_delta_kg: Decimal,
    ) -> PlanRecord:
        validate_quantity(quantity_delta_kg)
        if quantity_delta_kg > 0:
            raise ValueError("Планируемый расход должен иметь отрицательную дельту.")
        self._lock_material(material_id)
        transaction = self._transactions.add(
            TransactionType.CONSUMPTION, material_id, quantity_delta_kg,
        )
        query = (
            PostgreSQLQuery.into(Table("consumption"))
            .columns("transaction_id", "customer_id")
            .insert(Parameter("%s"), Parameter("%s"))
            .returning("id")
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (transaction.id, customer_id))
            plan_id = cursor.fetchone()[0]
        return PlanRecord(
            plan_id, transaction.id, transaction.transaction_type, material_id,
            transaction.quantity_delta_kg, customer_id, None, None, None,
            Decimal("0.00"), transaction.quantity_delta_kg.copy_abs(), False,
        )

    def add_receipt(
        self, material_id: int, supplier_id: int, order_date: date,
        planned_date: date, quantity_delta_kg: Decimal,
    ) -> PlanRecord:
        validate_quantity(quantity_delta_kg)
        if quantity_delta_kg < 0:
            raise ValueError("Планируемый приход должен иметь положительную дельту.")
        self._lock_material(material_id)
        transaction = self._transactions.add(
            TransactionType.PLANNED_RECEIPT, material_id, quantity_delta_kg,
        )
        query = (
            PostgreSQLQuery.into(Table("planned_receipts"))
            .columns("transaction_id", "supplier_id", "order_date", "planned_date")
            .insert(*(Parameter("%s") for _ in range(4)))
            .returning("id")
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (transaction.id, supplier_id, order_date, planned_date))
            plan_id = cursor.fetchone()[0]
        return PlanRecord(
            plan_id, transaction.id, transaction.transaction_type, material_id,
            transaction.quantity_delta_kg, None, supplier_id, order_date, planned_date,
            Decimal("0.00"), transaction.quantity_delta_kg, False,
        )

    def get_receipt(self, plan_id: int) -> PlanRecord | None:
        query = self._select(TransactionType.PLANNED_RECEIPT)
        return self._fetch_one(
            query.where(Table("planned_receipts").id == Parameter("%s")), (plan_id,),
        )

    def get_consumption(self, plan_id: int) -> PlanRecord | None:
        query = self._select(TransactionType.CONSUMPTION)
        return self._fetch_one(
            query.where(Table("consumption").id == Parameter("%s")), (plan_id,),
        )

    def get_by_transaction_id(self, transaction_id: int) -> PlanRecord | None:
        transaction = self._transactions.get(transaction_id)
        if transaction is None or transaction.transaction_type not in (
            TransactionType.CONSUMPTION.name, TransactionType.PLANNED_RECEIPT.name,
        ):
            return None
        kind = TransactionType[transaction.transaction_type]
        return self._fetch_one(
            self._select(kind).where(Table("transactions").id == Parameter("%s")),
            (transaction_id,),
        )

    def find_open_receipts(
        self, material_id: int | None = None, supplier_id: int | None = None, limit: int = 10,
    ) -> list[PlanRecord]:
        return self._find_open(TransactionType.PLANNED_RECEIPT, material_id, supplier_id, limit)

    def find_open_consumptions(
        self, material_id: int | None = None, customer_id: int | None = None, limit: int = 10,
    ) -> list[PlanRecord]:
        return self._find_open(TransactionType.CONSUMPTION, material_id, customer_id, limit)

    def cancel(
        self, plan_transaction_id: int, quantity_kg: Decimal, reason: str,
    ) -> PlanCancellationRecord:
        validate_quantity(quantity_kg)
        if quantity_kg < 0:
            raise ValueError("Отменяемое количество должно быть положительным.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Причина отмены не может быть пустой.")
        self._lock_plan(plan_transaction_id)
        plan = self.get_by_transaction_id(plan_transaction_id)
        if plan is None:
            raise ValueError(f"У плана transaction_id={plan_transaction_id} нет детализации.")
        if plan.is_closed:
            raise ValueError(f"План transaction_id={plan_transaction_id} уже закрыт.")
        if quantity_kg > plan.remaining_quantity_kg:
            raise ValueError("Отменяемое количество превышает открытый остаток плана.")
        query = (
            PostgreSQLQuery.into(Table("plan_cancellations"))
            .columns("plan_transaction_id", "quantity_kg", "reason")
            .insert(Parameter("%s"), Parameter("%s"), Parameter("%s"))
            .returning("id", "plan_transaction_id", "quantity_kg", "reason", "created_at")
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (plan_transaction_id, quantity_kg, reason))
            return PlanCancellationRecord(*cursor.fetchone())

    def _lock_material(self, material_id: int) -> None:
        query = (
            PostgreSQLQuery.from_(Table("materials"))
            .select("id").where(Table("materials").id == Parameter("%s"))
            .for_update()
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (material_id,))
            if cursor.fetchone() is None:
                raise ValueError(f"Материал id={material_id} не найден.")

    def _lock_plan(self, transaction_id: int) -> None:
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            raise ValueError(f"План transaction_id={transaction_id} не найден.")
        if transaction.transaction_type not in (
            TransactionType.CONSUMPTION.name, TransactionType.PLANNED_RECEIPT.name,
        ):
            raise ValueError(f"Транзакция id={transaction_id} не является планом.")
        self._lock_material(transaction.material_id)
        transactions = Table("transactions")
        query = (
            PostgreSQLQuery.from_(transactions).select(transactions.id)
            .where(transactions.id == Parameter("%s"))
            .where(transactions.material_id == Parameter("%s"))
            .where(transactions.transaction_type == Parameter("%s"))
            .for_update()
        )
        with self._connection.cursor() as cursor:
            cursor.execute(
                str(query), (transaction_id, transaction.material_id, transaction.transaction_type),
            )
            if cursor.fetchone() is None:
                raise ValueError(f"План transaction_id={transaction_id} изменился или удалён.")

    @staticmethod
    def _select(kind: TransactionType):
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

    def _find_open(
        self, kind: TransactionType, material_id: int | None, party_id: int | None, limit: int,
    ) -> list[PlanRecord]:
        plans = self._select(kind).as_("open_plans")
        query = (
            PostgreSQLQuery.from_(plans).select(plans.star)
            .where(plans.remaining_quantity_kg > 0)
        )
        params = []
        if material_id is not None:
            query = query.where(plans.material_id == Parameter("%s"))
            params.append(material_id)
        if party_id is not None:
            field = "customer_id" if kind == TransactionType.CONSUMPTION else "supplier_id"
            query = query.where(plans[field] == Parameter("%s"))
            params.append(party_id)
        query = query.orderby(plans.id).limit(Parameter("%s"))
        params.append(limit)
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), tuple(params))
            return [PlanRecord(*row) for row in cursor.fetchall()]

    def _fetch_one(self, query, params: tuple) -> PlanRecord | None:
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), params)
            row = cursor.fetchone()
        return PlanRecord(*row) if row else None
