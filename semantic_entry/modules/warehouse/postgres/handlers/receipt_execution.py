from __future__ import annotations

from typing import TYPE_CHECKING

from semantic_entry.modules.warehouse.commands.receipts import (
    ReceiptPreview,
    RecordReceiptInput,
    RecordReceiptResult,
)
from semantic_entry.modules.warehouse.postgres.handlers.receipt_preview import (
    ReceiptPreviewer,
)
from semantic_entry.modules.warehouse.postgres.repositories.catalog import (
    MaterialRepository,
)
from semantic_entry.modules.warehouse.postgres.repositories.locks import lock_material
from semantic_entry.modules.warehouse.postgres.repositories.movements import (
    MovementRepository,
)
from semantic_entry.modules.warehouse.postgres.repositories.plans import PlanRepository

if TYPE_CHECKING:
    from psycopg2.extensions import connection

    from semantic_entry.core.access import Actor
    from semantic_entry.core.commands.models import PreviewResult


class RecordReceiptHandler:
    def __init__(self, connection: connection):
        self._connection = connection

    def preview(self, parameters: RecordReceiptInput, actor: Actor) -> PreviewResult[ReceiptPreview]:
        previewer = ReceiptPreviewer(self._connection)
        try:
            lock_material(self._connection, parameters.material_id)
        except ValueError:
            return previewer.preview(parameters)
        material = MaterialRepository(self._connection).get(parameters.material_id)
        if parameters.planned_receipt_id is not None:
            plans = PlanRepository(self._connection)
            plan = plans.get_receipt(parameters.planned_receipt_id)
            if plan is not None and plan.material_id == parameters.material_id:
                plans.get_for_update(plan.transaction_id)
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT id FROM suppliers WHERE id = %s FOR SHARE", (parameters.supplier_id,))
            if material is not None:
                cursor.execute("SELECT id FROM film_types WHERE id = %s FOR SHARE", (material.film_type_id,))
        return previewer.preview(parameters)

    def execute(self, parameters: RecordReceiptInput, actor: Actor) -> RecordReceiptResult:
        parameters = RecordReceiptInput.model_validate(parameters.model_dump(round_trip=True))
        receipt = MovementRepository(self._connection).add_receipt(
            parameters.material_id, parameters.supplier_id,
            parameters.actual_date, parameters.quantity_kg,
            planned_receipt_id=parameters.planned_receipt_id,
        )
        return RecordReceiptResult(transaction_id=receipt.transaction_id, receipt_id=receipt.id)
