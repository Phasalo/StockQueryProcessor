from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from pydantic import Field, JsonValue, StrictInt, StringConstraints, model_validator

from semantic_entry.core.commands.models import CommandEffect, CommandModel

if TYPE_CHECKING:
    from typing import Self


@dataclass(frozen=True)
class ExtractionCommand:
    name: str
    version: int
    effect: CommandEffect
    description: str
    input_schema: dict[str, JsonValue]


class CommandCandidate(CommandModel):
    command_name: Annotated[str, StringConstraints(min_length=1, pattern=r"^[^\s@]+$")]
    command_version: StrictInt = Field(gt=0)
    parameters: dict[str, JsonValue]


class ExtractionStatus(StrEnum):
    CANDIDATE = "candidate"
    NEEDS_CLARIFICATION = "needs_clarification"
    NO_MATCH = "no_match"
    MULTIPLE_COMMANDS = "multiple_commands"


class ExtractionResult(CommandModel):
    status: ExtractionStatus
    candidate: CommandCandidate | None = None
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if (self.status == ExtractionStatus.CANDIDATE) != (self.candidate is not None):
            raise ValueError("Кандидат допустим только для статуса candidate и обязателен для него.")
        if self.status == ExtractionStatus.NEEDS_CLARIFICATION and self.message is None:
            raise ValueError("Для уточнения требуется сообщение пользователю.")
        return self


class ExtractionContractError(ValueError):
    pass
