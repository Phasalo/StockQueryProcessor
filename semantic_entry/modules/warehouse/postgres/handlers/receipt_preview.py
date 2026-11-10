from __future__ import annotations

from typing import TYPE_CHECKING

from semantic_entry.core.commands.models import FieldError, PreviewResult
from semantic_entry.modules.warehouse.commands.receipts import ReceiptPreview
from semantic_entry.modules.warehouse.postgres.repositories.catalog import (
    MaterialRepository,
    SupplierRepository,
)
from semantic_entry.modules.warehouse.postgres.repositories.plans import PlanRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg2.extensions import connection

    from semantic_entry.adapters.postgres.unit_of_work import UnitOfWork
    from semantic_entry.core.access import Actor
    from semantic_entry.modules.warehouse.commands.receipts import RecordReceiptInput


class ReceiptPreviewer:
    def __init__(self, connection: connection):
        self._connection = connection

    def preview(self, parameters: RecordReceiptInput) -> PreviewResult[ReceiptPreview]:
        material = MaterialRepository(self._connection).get(parameters.material_id)
        supplier = SupplierRepository(self._connection).get(parameters.supplier_id)
        errors = []
        if material is None:
            errors.append(FieldError(("material_id",), "not_found", "Материал не найден."))
        if supplier is None:
            errors.append(FieldError(("supplier_id",), "not_found", "Поставщик не найден."))
        plan = None
        if parameters.planned_receipt_id is not None:
            plan = PlanRepository(self._connection).get_receipt(parameters.planned_receipt_id)
            if plan is None:
                errors.append(FieldError(("planned_receipt_id",), "not_found", "План прихода не найден."))
            else:
                if plan.material_id != parameters.material_id:
                    errors.append(FieldError(("material_id",), "plan_material_mismatch", "Материал не совпадает с материалом плана."))
                if plan.supplier_id != parameters.supplier_id:
                    errors.append(FieldError(("supplier_id",), "plan_supplier_mismatch", "Поставщик не совпадает с поставщиком плана."))
                if plan.is_closed:
                    errors.append(FieldError(("planned_receipt_id",), "plan_closed", "План прихода уже закрыт."))
        if errors:
            return PreviewResult[ReceiptPreview](errors=tuple(errors))
        return PreviewResult[ReceiptPreview](preview=ReceiptPreview(
            parameters=parameters,
            film_type_code=material.film_type_code,
            brand=material.brand,
            format=material.format,
            thickness=material.thickness,
            supplier_name=supplier.name,
            plan_remaining_quantity_kg=plan.remaining_quantity_kg if plan is not None else None,
        ))


class ReceiptPreviewHandler:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]):
        self._uow_factory = uow_factory

    def __call__(self, parameters: RecordReceiptInput, actor: Actor) -> PreviewResult[ReceiptPreview]:
        with self._uow_factory() as uow:
            uow.connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
            return ReceiptPreviewer(uow.connection).preview(parameters)
