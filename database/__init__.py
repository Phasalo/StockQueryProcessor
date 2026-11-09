from contextlib import closing
from dataclasses import asdict
from datetime import date
from decimal import Decimal

import psycopg2
from pypika import Table, Parameter, PostgreSQLQuery

from config import WarehouseConfig
from database.enums import FilmType, TransactionType
from database.repositories.catalog import (
    CustomerRepository, FilmTypeRepository, MaterialRecord, MaterialRepository,
    SupplierRepository,
)
from database.repositories.plans import PlanCancellationRecord, PlanRecord, PlanRepository
from database.unit_of_work import UnitOfWork


class Warehouse:
    def __init__(self, config: WarehouseConfig):
        self._config = config
        self._dsn = f"postgresql://{config.user}:{config.password}@{config.host}:{config.port}/{config.database}"

    def add_film(
        self, film_type: FilmType | str, brand: str, film_format: int, thickness: int,
    ) -> int:
        code = film_type.name if isinstance(film_type, FilmType) else film_type
        with UnitOfWork(self._config) as uow:
            record = FilmTypeRepository(uow.connection).find_by_code(code)
            if record is None:
                raise ValueError(f"Неизвестный тип плёнки: {code}.")
            material_id = MaterialRepository(uow.connection).add(
                record.id, brand, film_format, thickness,
            )
        return material_id

    def find_film(
        self, film_type: FilmType | str, brand: str, film_format: int, thickness: int,
    ) -> int | None:
        code = film_type.name if isinstance(film_type, FilmType) else film_type
        with UnitOfWork(self._config) as uow:
            record = FilmTypeRepository(uow.connection).find_by_code(code)
            if record is None:
                return None
            material = MaterialRepository(uow.connection).find(
                record.id, brand, film_format, thickness,
            )
        return material.id if material else None

    def search_films(
        self,
        film_type: FilmType | str | None = None,
        brand: str | None = None,
        film_format: int | None = None,
        thickness: int | None = None,
        limit: int = 10,
    ) -> list[dict]:
        with UnitOfWork(self._config) as uow:
            film_type_id = None
            if film_type is not None:
                code = film_type.name if isinstance(film_type, FilmType) else film_type
                record = FilmTypeRepository(uow.connection).find_by_code(code)
                if record is None:
                    return []
                film_type_id = record.id
            materials = MaterialRepository(uow.connection).search(
                film_type_id, brand, film_format, thickness, limit,
            )
        return [self._film_result(material) for material in materials]

    def get_film(self, film_id: int) -> dict | None:
        with UnitOfWork(self._config) as uow:
            material = MaterialRepository(uow.connection).get(film_id)
        return self._film_result(material) if material else None

    @staticmethod
    def _film_result(material: MaterialRecord) -> dict:
        return {
            "id": material.id,
            "type": material.film_type_code,
            "brand": material.brand,
            "format": material.format,
            "thickness": material.thickness,
        }

    def add_customer(self, name: str, prod_card_number: str) -> int:
        with UnitOfWork(self._config) as uow:
            customer_id = CustomerRepository(uow.connection).add(name, prod_card_number)
        return customer_id

    def find_customer(self, name: str, prod_card_number: str) -> int | None:
        with UnitOfWork(self._config) as uow:
            customer = CustomerRepository(uow.connection).find(name, prod_card_number)
        return customer.id if customer else None

    def search_customers(self, query: str, limit: int = 10) -> list[dict]:
        with UnitOfWork(self._config) as uow:
            customers = CustomerRepository(uow.connection).search(query, limit)
        return [asdict(customer) for customer in customers]

    def get_customer(self, customer_id: int) -> dict | None:
        with UnitOfWork(self._config) as uow:
            customer = CustomerRepository(uow.connection).get(customer_id)
        return asdict(customer) if customer else None

    def add_supplier(self, name: str) -> int:
        with UnitOfWork(self._config) as uow:
            supplier_id = SupplierRepository(uow.connection).add(name)
        return supplier_id

    def find_supplier(self, name: str) -> int | None:
        with UnitOfWork(self._config) as uow:
            supplier = SupplierRepository(uow.connection).find(name)
        return supplier.id if supplier else None

    def search_suppliers(self, query: str, limit: int = 10) -> list[dict]:
        with UnitOfWork(self._config) as uow:
            suppliers = SupplierRepository(uow.connection).search(query, limit)
        return [asdict(supplier) for supplier in suppliers]

    def get_supplier(self, supplier_id: int) -> dict | None:
        with UnitOfWork(self._config) as uow:
            supplier = SupplierRepository(uow.connection).get(supplier_id)
        return asdict(supplier) if supplier else None

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
        """Возвращает ID журнала; ID плана доступен через PlanRepository."""
        with UnitOfWork(self._config) as uow:
            plan = PlanRepository(uow.connection).add_consumption(film_id, customer_id, delta_kg)
        return plan.transaction_id

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

    def create_planned_receipt(
        self, film_id: int, supplier_id: int, planned_date: date, delta_kg: Decimal,
        *, order_date: date,
    ) -> int:
        """Возвращает ID журнала; ID плана доступен через PlanRepository."""
        with UnitOfWork(self._config) as uow:
            plan = PlanRepository(uow.connection).add_receipt(
                film_id, supplier_id, order_date, planned_date, delta_kg,
            )
        return plan.transaction_id

    def find_open_planned_receipts(
        self, film_id: int | None = None, supplier_id: int | None = None, limit: int = 10,
    ) -> list[dict]:
        with UnitOfWork(self._config) as uow:
            plans = PlanRepository(uow.connection).find_open_receipts(film_id, supplier_id, limit)
        return [self._planned_receipt_result(plan) for plan in plans]

    def get_planned_receipt(self, planned_receipt_id: int) -> dict | None:
        with UnitOfWork(self._config) as uow:
            plan = PlanRepository(uow.connection).get_receipt(planned_receipt_id)
        return self._planned_receipt_result(plan) if plan else None

    def cancel_plan(
        self, plan_transaction_id: int, quantity_kg: Decimal, reason: str,
    ) -> PlanCancellationRecord:
        with UnitOfWork(self._config) as uow:
            cancellation = PlanRepository(uow.connection).cancel(
                plan_transaction_id, quantity_kg, reason,
            )
        return cancellation

    @staticmethod
    def _planned_receipt_result(plan: PlanRecord) -> dict:
        return {
            "id": plan.id,
            "transaction_id": plan.transaction_id,
            "film_id": plan.material_id,
            "supplier_id": plan.supplier_id,
            "order_date": plan.order_date,
            "planned_date": plan.planned_date,
            "delta_kg": plan.quantity_delta_kg,
            "cancelled_quantity_kg": plan.cancelled_quantity_kg,
            "remaining_quantity_kg": plan.remaining_quantity_kg,
            "is_closed": plan.is_closed,
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
