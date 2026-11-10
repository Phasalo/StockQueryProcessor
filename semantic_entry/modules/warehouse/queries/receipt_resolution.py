from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Generic, TypeVar

from pydantic import Field, StrictInt, StringConstraints, model_validator

from semantic_entry.core.commands.models import CommandEffect, CommandModel, CommandSpec
from semantic_entry.modules.warehouse.commands.receipts import RECORD_RECEIPT_SPEC
from semantic_entry.modules.warehouse.models import MaterialRecord, SupplierRecord

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Self


class MaterialLookup(CommandModel):
    film_type_code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)] | None = None
    brand: str | None = Field(default=None, max_length=255)
    format: StrictInt | None = Field(default=None, gt=0)
    thickness: StrictInt | None = Field(default=None, gt=0)

    @property
    def has_criteria(self) -> bool:
        return any(value is not None for value in (self.film_type_code, self.brand, self.format, self.thickness))


class ResolveReceiptInput(CommandModel):
    material: MaterialLookup = Field(default_factory=MaterialLookup)
    supplier_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None
    limit: StrictInt = Field(default=10, ge=1, le=100)


class ResolutionStatus(StrEnum):
    MISSING = "missing"
    NOT_FOUND = "not_found"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"


CandidateT = TypeVar("CandidateT", MaterialRecord, SupplierRecord)


class ReferenceResolution(CommandModel, Generic[CandidateT]):
    status: ResolutionStatus = ResolutionStatus.MISSING
    candidates: tuple[CandidateT, ...] = ()
    has_more: bool = False

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status in (ResolutionStatus.MISSING, ResolutionStatus.NOT_FOUND):
            valid = not self.candidates and not self.has_more
        elif self.status == ResolutionStatus.RESOLVED:
            valid = len(self.candidates) == 1 and not self.has_more
        else:
            valid = bool(self.candidates) and (len(self.candidates) > 1 or self.has_more)
        if not valid:
            raise ValueError("Статус разрешения не соответствует списку кандидатов.")
        return self

    @classmethod
    def from_candidates(cls, candidates: Sequence[CandidateT], *, limit: int) -> Self:
        if not candidates:
            status = ResolutionStatus.NOT_FOUND
        elif len(candidates) == 1:
            status = ResolutionStatus.RESOLVED
        else:
            status = ResolutionStatus.AMBIGUOUS
        return cls(status=status, candidates=tuple(candidates[:limit]), has_more=len(candidates) > limit)

    @property
    def resolved_id(self) -> int | None:
        return self.candidates[0].id if self.status == ResolutionStatus.RESOLVED else None


class ResolveReceiptResult(CommandModel):
    material: ReferenceResolution[MaterialRecord] = Field(default_factory=ReferenceResolution[MaterialRecord])
    supplier: ReferenceResolution[SupplierRecord] = Field(default_factory=ReferenceResolution[SupplierRecord])

    @property
    def resolved_ids(self) -> dict[str, int]:
        return {
            name: reference.resolved_id
            for name, reference in (("material_id", self.material), ("supplier_id", self.supplier))
            if reference.resolved_id is not None
        }


RESOLVE_RECEIPT_SPEC = CommandSpec(
    name="warehouse.resolve_receipt",
    version=1,
    effect=CommandEffect.READ,
    input_model=ResolveReceiptInput,
    result_model=ResolveReceiptResult,
    description="Найти материал и поставщика для прихода; вернуть однозначные ID или кандидатов для выбора.",
    permission=RECORD_RECEIPT_SPEC.permission,
)
