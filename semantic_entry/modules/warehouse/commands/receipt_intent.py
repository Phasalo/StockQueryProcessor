from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StringConstraints

from semantic_entry.core.commands.models import CommandModel
from semantic_entry.modules.warehouse.models import MaterialLookup


class ReceiptIntent(CommandModel):
    material: MaterialLookup = Field(default_factory=MaterialLookup)
    supplier_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None
    actual_date: date | None = None
    quantity_kg: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2, allow_inf_nan=False)
