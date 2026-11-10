from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import ValidationError

from semantic_entry.core.commands.models import (
    CommandEffect,
    CommandModel,
    CommandSpec,
    FieldError,
    PreparationResult,
    PreviewResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from semantic_entry.core.access import Actor
    from semantic_entry.core.commands.catalog import CommandCatalog


InputT = TypeVar("InputT", bound=CommandModel)
PreviewT = TypeVar("PreviewT", bound=CommandModel)


@dataclass(frozen=True)
class _PreviewBinding:
    spec: CommandSpec[Any, Any]
    preview_model: type[CommandModel]
    handler: Callable[[Any, Actor], PreviewResult[Any]]


def validate_preview(result: object, preview_model: type[PreviewT]) -> PreviewResult[PreviewT]:
    expected_model = PreviewResult[preview_model]
    if not isinstance(result, expected_model):
        raise TypeError("Обработчик preview вернул результат другой схемы.")
    checked = expected_model.model_validate(result.model_dump(round_trip=True))
    if (
        (checked.preview is None and not checked.errors)
        or (checked.preview is not None and checked.errors)
    ):
        raise ValueError("Обработчик preview должен вернуть карточку либо ошибки.")
    return checked


class CommandPreparer:
    def __init__(self, catalog: CommandCatalog):
        self._catalog = catalog
        self._previewers: dict[tuple[str, int], _PreviewBinding] = {}

    def register_preview(
        self, spec: CommandSpec[InputT, Any], preview_model: type[PreviewT],
        handler: Callable[[InputT, Actor], PreviewResult[PreviewT]],
    ) -> None:
        if not isinstance(spec, CommandSpec):
            raise TypeError("Для регистрации preview требуется CommandSpec.")
        if spec.effect != CommandEffect.WRITE:
            raise ValueError("Preview регистрируется только для команд WRITE.")
        if not isinstance(preview_model, type) or not issubclass(preview_model, CommandModel):
            raise TypeError("Схема preview должна быть классом CommandModel.")
        if preview_model.model_config.get("extra") != "forbid":
            raise ValueError("Схема preview должна запрещать лишние поля.")
        if not callable(handler):
            raise TypeError("Обработчик preview должен быть вызываемым объектом.")
        key = spec.name, spec.version
        if key in self._previewers:
            raise ValueError(f"Preview для {spec.key} уже зарегистрирован.")
        self._previewers[key] = _PreviewBinding(spec, preview_model, handler)

    def prepare(
        self, name: str, version: int, payload: object, actor: Actor,
    ) -> PreparationResult:
        spec = self._catalog.get(name, version, actor)
        binding = self._previewers.get((spec.name, spec.version))
        if binding is not None and binding.spec is not spec:
            raise ValueError("Preview зарегистрирован для другой спецификации команды.")
        if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
            return PreparationResult(
                spec.name, spec.version, actor.actor_id, None,
                (FieldError((), "mapping_type", "Параметры команды должны быть объектом со строковыми ключами."),),
            )
        try:
            validated = spec.validate_input(payload)
        except ValidationError as error:
            issues = tuple(
                FieldError(tuple(item["loc"]), item["type"], item["msg"])
                for item in error.errors()
            )
            return PreparationResult(spec.name, spec.version, actor.actor_id, None, issues)
        result = PreparationResult(spec.name, spec.version, actor.actor_id, validated)
        if binding is None:
            return result
        preview = binding.handler(validated, actor)
        checked = validate_preview(preview, binding.preview_model)
        return replace(result, errors=checked.errors, preview=checked.preview)
