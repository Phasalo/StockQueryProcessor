from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from semantic_entry.core.commands.models import CommandEffect, CommandModel, CommandSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from semantic_entry.core.access import Actor
    from semantic_entry.core.commands.catalog import CommandCatalog


InputT = TypeVar("InputT", bound=CommandModel)
ResultT = TypeVar("ResultT", bound=CommandModel)


@dataclass(frozen=True)
class _QueryBinding:
    spec: CommandSpec[Any, Any]
    handler: Callable[[Any, Actor], CommandModel]


class QueryExecutor:
    def __init__(self, catalog: CommandCatalog):
        self._catalog = catalog
        self._handlers: dict[tuple[str, int], _QueryBinding] = {}

    def register(
        self, spec: CommandSpec[InputT, ResultT], handler: Callable[[InputT, Actor], ResultT],
    ) -> None:
        if not isinstance(spec, CommandSpec):
            raise TypeError("Для регистрации обработчика требуется CommandSpec.")
        if spec.effect != CommandEffect.READ:
            raise ValueError("Обработчик запросов поддерживает только команды READ.")
        if not callable(handler):
            raise TypeError("Обработчик запроса должен быть вызываемым объектом.")
        key = spec.name, spec.version
        if key in self._handlers:
            raise ValueError(f"Обработчик {spec.key} уже зарегистрирован.")
        self._handlers[key] = _QueryBinding(spec, handler)

    def query(
        self, name: str, version: int, payload: Mapping[str, Any], actor: Actor,
    ) -> CommandModel:
        spec = self._catalog.get(name, version, actor)
        if spec.effect != CommandEffect.READ:
            raise ValueError("Через query можно выполнять только команды READ.")
        binding = self._handlers[name, version]
        if binding.spec is not spec:
            raise ValueError("Обработчик зарегистрирован для другой спецификации команды.")
        if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
            raise TypeError("Параметры запроса должны быть объектом со строковыми ключами.")
        parameters = spec.validate_input(payload)
        return spec.validate_handler_result(binding.handler(parameters, actor))
