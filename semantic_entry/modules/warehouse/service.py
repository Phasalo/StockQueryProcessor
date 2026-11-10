from dataclasses import asdict
from datetime import date
from decimal import Decimal

from config import WarehouseConfig
from semantic_entry.modules.warehouse.enums import FilmType, TransactionType
from semantic_entry.modules.warehouse.postgres.catalog import (
    CustomerRepository,
    FilmTypeRepository,
    MaterialRecord,
    MaterialRepository,
    SupplierRepository,
)
from semantic_entry.modules.warehouse.postgres.locks import lock_material
from semantic_entry.modules.warehouse.postgres.movements import MovementRepository
from semantic_entry.modules.warehouse.postgres.plans import (
    PlanCancellationRecord,
    PlanRecord,
    PlanRepository,
)
from semantic_entry.modules.warehouse.postgres.transactions import (
    TransactionRepository,
    validate_quantity,
)
from semantic_entry.modules.warehouse.postgres.unit_of_work import UnitOfWork


class Warehouse:
    def __init__(self, config: WarehouseConfig):
        self._config = config

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

    def create_consumption(self, film_id: int, customer_id: int, delta_kg: Decimal) -> int:
        """Возвращает ID журнала; ID плана доступен через PlanRepository."""
        with UnitOfWork(self._config) as uow:
            plan = PlanRepository(uow.connection).add_consumption(film_id, customer_id, delta_kg)
        return plan.transaction_id

    def create_adjustment(self, film_id: int, delta_kg: Decimal) -> int:
        validate_quantity(delta_kg)
        with UnitOfWork(self._config) as uow:
            lock_material(uow.connection, film_id)
            transaction = TransactionRepository(uow.connection).add(
                TransactionType.ADJUSTMENT, film_id, delta_kg,
            )
        return transaction.id

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

    def create_receipt(
        self, film_id: int, planned_receipt_id: int | None,
        actual_date: date, delta_kg: Decimal, *, supplier_id: int | None = None,
    ) -> int:
        with UnitOfWork(self._config) as uow:
            if supplier_id is None:
                if planned_receipt_id is None:
                    raise ValueError("Для прихода без плана требуется supplier_id.")
                plan = PlanRepository(uow.connection).get_receipt(planned_receipt_id)
                if plan is None:
                    raise ValueError(f"Плановый приход id={planned_receipt_id} не найден.")
                supplier_id = plan.supplier_id
            movement = MovementRepository(uow.connection).add_receipt(
                film_id, supplier_id, actual_date, delta_kg,
                planned_receipt_id=planned_receipt_id,
            )
        return movement.transaction_id

    def create_issue(
        self, film_id: int, customer_id: int, actual_date: date | None,
        delta_kg: Decimal, *, planned_consumption_id: int | None = None,
    ) -> int:
        with UnitOfWork(self._config) as uow:
            movement = MovementRepository(uow.connection).add_issue(
                film_id, customer_id, actual_date, delta_kg,
                planned_consumption_id=planned_consumption_id,
            )
        return movement.transaction_id
