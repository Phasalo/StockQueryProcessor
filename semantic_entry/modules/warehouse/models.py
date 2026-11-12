from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StrictInt, StringConstraints

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


class MaterialLookup(CommandModel):
    film_type_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)] | None = Field(
        default=None, description="Код типа плёнки, например BOPP или CPP. Тип плёнки и её марка — разные поля.",
    )
    brand: str | None = Field(default=None, max_length=255, description="Марка плёнки, отдельно от кода типа.")
    format: StrictInt | None = Field(default=None, gt=0, description="Ширина плёнки в миллиметрах.")
    thickness: StrictInt | None = Field(default=None, gt=0, description="Толщина плёнки в микрометрах.")

    @property
    def has_criteria(self) -> bool:
        return any(value is not None for value in (self.film_type_code, self.brand, self.format, self.thickness))
