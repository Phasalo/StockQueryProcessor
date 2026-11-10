from datetime import date
from decimal import Decimal

from pydantic import Field, StrictInt

from semantic_entry.core.commands.models import CommandEffect, CommandModel, CommandSpec


class RecordReceiptInput(CommandModel):
    material_id: StrictInt = Field(gt=0)
    supplier_id: StrictInt = Field(gt=0)
    actual_date: date
    quantity_kg: Decimal = Field(gt=0, max_digits=10, decimal_places=2, allow_inf_nan=False)
    planned_receipt_id: StrictInt | None = Field(default=None, gt=0)


class RecordReceiptResult(CommandModel):
    transaction_id: StrictInt = Field(gt=0)
    receipt_id: StrictInt = Field(gt=0)


class ReceiptPreview(CommandModel):
    parameters: RecordReceiptInput
    film_type_code: str
    brand: str
    format: StrictInt = Field(gt=0)
    thickness: StrictInt = Field(gt=0)
    supplier_name: str
    plan_remaining_quantity_kg: Decimal | None = None


RECORD_RECEIPT_SPEC = CommandSpec(
    name="warehouse.record_receipt",
    version=1,
    effect=CommandEffect.WRITE,
    input_model=RecordReceiptInput,
    result_model=RecordReceiptResult,
    description="Записать фактический приход материала от поставщика, с необязательной ссылкой на план прихода.",
    permission="warehouse.receipt.write",
)
