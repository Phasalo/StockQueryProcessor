from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict


class CommandEffect(str, Enum):
    READ = "READ"
    WRITE = "WRITE"


@dataclass(frozen=True)
class Actor:
    actor_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValueError("actor_id должен быть непустой строкой.")


class PermissionDenied(PermissionError):
    pass


class CommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


InputT = TypeVar("InputT", bound=CommandModel)
ResultT = TypeVar("ResultT", bound=CommandModel)


@dataclass(frozen=True)
class CommandSpec(Generic[InputT, ResultT]):
    name: str
    version: int
    effect: CommandEffect
    input_model: type[InputT]
    result_model: type[ResultT]
    description: str
    permission: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str) or not self.name or "@" in self.name
            or any(char.isspace() for char in self.name)
        ):
            raise ValueError("Имя команды должно быть непустым, без пробелов и @.")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("Версия команды должна быть положительным целым числом.")
        if not isinstance(self.effect, CommandEffect):
            raise TypeError("Эффект команды должен быть CommandEffect.")
        for value in (self.description, self.permission):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Описание и разрешение команды не могут быть пустыми.")
        for model in (self.input_model, self.result_model):
            if not isinstance(model, type) or not issubclass(model, CommandModel):
                raise TypeError("Схемы команды должны быть классами CommandModel.")
            if model.model_config.get("extra") != "forbid":
                raise ValueError("Схемы команды должны запрещать лишние поля.")

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def validate_input(self, payload: Mapping[str, Any]) -> InputT:
        return self.input_model.model_validate(dict(payload))

    def validate_result(self, payload: Mapping[str, Any]) -> ResultT:
        return self.result_model.model_validate(dict(payload))

    def input_json_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def result_json_schema(self) -> dict[str, Any]:
        return self.result_model.model_json_schema()
