from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from semantic_entry.modules.warehouse.enums import FilmType, TransactionType
from semantic_entry.modules.warehouse.postgres.reports import ReportRepository
from semantic_entry.modules.warehouse.postgres.unit_of_work import UnitOfWork

if TYPE_CHECKING:
    from config import WarehouseConfig
    from semantic_entry.modules.warehouse.models import StockBalance


class WarehouseReports:
    def __init__(self, config: WarehouseConfig):
        self._config = config

    @staticmethod
    def _type_code(film_type: FilmType | str) -> str:
        return film_type.name if isinstance(film_type, FilmType) else film_type

    @staticmethod
    def _stock_result(row: StockBalance) -> dict:
        return {
            "material_id": row.material_id, "brand": row.brand,
            "format": row.format, "thickness": row.thickness,
            "on_hand_kg": row.on_hand_kg, "reserved_kg": row.reserved_kg,
            "planned_consumption_kg": row.reserved_kg.copy_negate(),
            "planned_receipt_kg": row.planned_receipt_kg,
            "free_balance_kg": row.available_kg, "available_kg": row.available_kg,
            "projected_kg": row.projected_kg,
        }

    def _find_stock(self, repository, film_type, film_format, thickness, brand):
        rows = repository.list_stock(
            self._type_code(film_type), film_format=film_format, thickness=thickness, brand=brand,
        )
        if len(rows) > 1:
            raise ValueError("Для этой комбинации найдено несколько марок. Укажите brand.")
        return rows[0] if rows else None

    def get_film_type_summary(self, film_type: FilmType | str) -> list[dict]:
        with UnitOfWork(self._config) as uow:
            rows = ReportRepository(uow.connection).list_stock(self._type_code(film_type))
        return [self._stock_result(row) for row in rows]

    def get_film_summary(
        self, film_type: FilmType | str, film_format: int, thickness: int,
        *, brand: str | None = None,
    ) -> dict:
        with UnitOfWork(self._config) as uow:
            row = self._find_stock(ReportRepository(uow.connection), film_type, film_format, thickness, brand)
        if row is not None:
            return self._stock_result(row)
        return {key: Decimal("0.00") for key in (
            "planned_consumption_kg", "planned_receipt_kg", "free_balance_kg",
            "on_hand_kg", "reserved_kg", "available_kg", "projected_kg",
        )}

    def get_planned_consumption_details(
        self, film_type: FilmType | str, film_format: int, thickness: int,
        *, brand: str | None = None,
    ) -> list[dict]:
        with UnitOfWork(self._config) as uow:
            repository = ReportRepository(uow.connection)
            material = self._find_stock(repository, film_type, film_format, thickness, brand)
            rows = repository.list_open_consumption(material.material_id) if material else []
        return [{
            "plan_id": row.plan_id, "transaction_id": row.transaction_id,
            "customer_name": row.customer_name, "prod_card_number": row.prod_card_number,
            "delta_kg": row.remaining_quantity_kg.copy_negate(),
        } for row in rows]

    def get_planned_receipt_details(
        self, film_type: FilmType | str, film_format: int, thickness: int,
        *, brand: str | None = None,
    ) -> list[dict]:
        with UnitOfWork(self._config) as uow:
            repository = ReportRepository(uow.connection)
            material = self._find_stock(repository, film_type, film_format, thickness, brand)
            rows = repository.list_open_receipts(material.material_id) if material else []
        return [{
            "plan_id": row.plan_id, "transaction_id": row.transaction_id,
            "supplier_name": row.supplier_name, "order_date": row.order_date,
            "planned_date": row.planned_date, "delta_kg": row.remaining_quantity_kg,
        } for row in rows]

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

    def get_available_formats(self, film_type: FilmType | str) -> list[int]:
        with UnitOfWork(self._config) as uow:
            return ReportRepository(uow.connection).list_formats(self._type_code(film_type))

    def get_available_thicknesses(self, film_type: FilmType | str) -> list[int]:
        with UnitOfWork(self._config) as uow:
            return ReportRepository(uow.connection).list_thicknesses(self._type_code(film_type))

    def get_available_brands(self, film_type: FilmType | str, film_format: int, thickness: int) -> list[str]:
        with UnitOfWork(self._config) as uow:
            return ReportRepository(uow.connection).list_brands(self._type_code(film_type), film_format, thickness)
