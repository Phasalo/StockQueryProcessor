from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, localcontext

from psycopg2.extensions import connection
from pypika import Parameter, PostgreSQLQuery, Table

from database.enums import TransactionType


@dataclass(frozen=True)
class TransactionRecord:
    id: int
    created_at: datetime
    transaction_type: str
    material_id: int
    quantity_delta_kg: Decimal


class TransactionRepository:
    def __init__(self, connection: connection):
        self._connection = connection

    def add(
        self,
        transaction_type: TransactionType | str,
        material_id: int,
        quantity_delta_kg: Decimal,
    ) -> TransactionRecord:
        self._validate_quantity(quantity_delta_kg)
        code = (
            transaction_type.name
            if isinstance(transaction_type, TransactionType)
            else transaction_type
        )
        transactions = Table("transactions")
        query = (
            PostgreSQLQuery.into(transactions)
            .columns("transaction_type", "material_id", "quantity_delta_kg")
            .insert(Parameter("%s"), Parameter("%s"), Parameter("%s"))
            .returning(
                "id", "created_at", "transaction_type", "material_id", "quantity_delta_kg",
            )
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (code, material_id, quantity_delta_kg))
            return TransactionRecord(*cursor.fetchone())

    def get(self, transaction_id: int) -> TransactionRecord | None:
        transactions = Table("transactions")
        query = (
            PostgreSQLQuery.from_(transactions)
            .select(
                transactions.id, transactions.created_at, transactions.transaction_type,
                transactions.material_id, transactions.quantity_delta_kg,
            )
            .where(transactions.id == Parameter("%s"))
        )
        with self._connection.cursor() as cursor:
            cursor.execute(str(query), (transaction_id,))
            row = cursor.fetchone()
        return TransactionRecord(*row) if row else None

    @staticmethod
    def _validate_quantity(value: Decimal) -> None:
        if not isinstance(value, Decimal):
            raise TypeError("quantity_delta_kg должен быть Decimal.")
        if not value.is_finite() or value == 0:
            raise ValueError("quantity_delta_kg должен быть конечным ненулевым числом.")
        if value.copy_abs() > Decimal("99999999.99"):
            raise ValueError("quantity_delta_kg не помещается в decimal(10,2).")
        with localcontext(Context(prec=10)):
            if value.quantize(Decimal("0.01")) != value:
                raise ValueError("quantity_delta_kg допускает не более двух знаков после запятой.")
