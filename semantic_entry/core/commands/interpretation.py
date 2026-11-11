from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from semantic_entry.core.access import PermissionDeniedError
from semantic_entry.core.commands.extraction import (
    ExtractionCommand,
    ExtractionContractError,
    ExtractionResult,
)
from semantic_entry.core.commands.models import CommandModel, CommandSpec

if TYPE_CHECKING:
    from semantic_entry.core.access import Actor
    from semantic_entry.core.commands.catalog import CommandCatalog
    from semantic_entry.core.ports.extraction import CommandExtractor


@dataclass(frozen=True)
class _ExtractionBinding:
    spec: CommandSpec[Any, Any]
    input_model: type[CommandModel]


class CommandInterpreter:
    def __init__(self, catalog: CommandCatalog, extractor: CommandExtractor | None):
        self._catalog = catalog
        self._extractor = extractor
        self._bindings: dict[tuple[str, int], _ExtractionBinding] = {}

    def register(self, spec: CommandSpec[Any, Any], input_model: type[CommandModel]) -> None:
        if not isinstance(spec, CommandSpec):
            raise TypeError("Для регистрации извлечения требуется CommandSpec.")
        if not isinstance(input_model, type) or not issubclass(input_model, CommandModel):
            raise TypeError("Схема извлечения должна быть классом CommandModel.")
        if input_model.model_config.get("extra") != "forbid":
            raise ValueError("Схема извлечения должна запрещать лишние поля.")
        key = spec.name, spec.version
        if key in self._bindings:
            raise ValueError(f"Извлечение {spec.key} уже зарегистрировано.")
        self._bindings[key] = _ExtractionBinding(spec, input_model)

    def interpret(self, text: str, actor: Actor) -> ExtractionResult:
        if not isinstance(text, str):
            raise TypeError("Для извлечения требуется текст.")
        if not text.strip():
            raise ValueError("Текст не может быть пустым.")
        if self._extractor is None:
            raise RuntimeError("Извлечение команд из текста не настроено.")
        allowed = self._allowed(actor)
        commands = tuple(ExtractionCommand(
            binding.spec.name, binding.spec.version, binding.spec.effect,
            binding.spec.description, binding.input_model.model_json_schema(),
        ) for binding in allowed.values())
        result = self._extractor.extract(text, commands)
        if not isinstance(result, ExtractionResult):
            raise ExtractionContractError("Извлечение должно вернуть ExtractionResult.")
        try:
            checked = ExtractionResult.model_validate(result.model_dump(round_trip=True))
        except ValidationError as error:
            raise ExtractionContractError("Извлечение вернуло некорректный результат.") from error
        if checked.candidate is None:
            return checked
        candidate = checked.candidate
        binding = allowed.get((candidate.command_name, candidate.command_version))
        if binding is None:
            raise ExtractionContractError("Извлечена команда вне переданного каталога.")
        self._catalog.get(candidate.command_name, candidate.command_version, actor)
        try:
            parameters = binding.input_model.model_validate(candidate.parameters)
        except ValidationError as error:
            raise ExtractionContractError("Параметры кандидата не соответствуют схеме извлечения.") from error
        return checked.model_copy(update={"candidate": candidate.model_copy(update={
            "parameters": parameters.model_dump(mode="json", round_trip=True, exclude_unset=True),
        })})

    def _allowed(self, actor: Actor) -> dict[tuple[str, int], _ExtractionBinding]:
        allowed = {}
        for spec in self._catalog.list(actor):
            key = spec.name, spec.version
            binding = self._bindings.get(key)
            if binding is None:
                continue
            if binding.spec is not spec:
                raise ValueError("Извлечение зарегистрировано для другой спецификации команды.")
            allowed[key] = binding
        if not allowed:
            raise PermissionDeniedError("Нет доступных команд для извлечения из текста.")
        return allowed
