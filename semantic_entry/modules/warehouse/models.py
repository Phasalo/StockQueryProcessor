from dataclasses import dataclass
from decimal import Decimal

from pydantic import Field, StrictInt

from semantic_entry.core.commands.models import CommandModel


class StockBalance(CommandModel):
    material_id: StrictInt = Field(gt=0)
    film_type_code: str
    film_type_name: str
    brand: str
    format: StrictInt = Field(gt=0)
    thickness: StrictInt = Field(gt=0)
    on_hand_kg: Decimal
    reserved_kg: Decimal
    planned_receipt_kg: Decimal
    available_kg: Decimal
    projected_kg: Decimal


@dataclass(frozen=True)
class MaterialRecord:
    id: int
    film_type_id: int
    film_type_code: str
    brand: str
    format: int
    thickness: int


@dataclass(frozen=True)
class SupplierRecord:
    id: int
    name: str
