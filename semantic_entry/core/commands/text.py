from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

from semantic_entry.core.commands.models import CommandEffect, CommandModel, CommandSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from semantic_entry.core.access import Actor
    from semantic_entry.core.commands.catalog import CommandCatalog
    from semantic_entry.core.commands.extraction import ExtractionResult
    from semantic_entry.core.commands.interpretation import CommandInterpreter
    from semantic_entry.core.drafts.models import CommandDraft


@dataclass(frozen=True)
class CandidateNormalization:
    parameters: Mapping[str, object]
    resolution: CommandModel | None = None


@dataclass(frozen=True)
class TextPreparationResult:
    extraction: ExtractionResult
    draft: CommandDraft | None = None
    resolution: CommandModel | None = None


class _PrepareCommand(Protocol):
    def __call__(
        self, name: str, version: int, payload: Mapping[str, object], actor: Actor,
        *, lifetime: timedelta,
    ) -> CommandDraft: ...


@dataclass(frozen=True)
class _NormalizationBinding:
    spec: CommandSpec[Any, Any]
    normalize: Callable[[Mapping[str, object], Actor], CandidateNormalization]


class CommandTextPreparer:
    def __init__(
        self, catalog: CommandCatalog, interpreter: CommandInterpreter, prepare: _PrepareCommand,
    ):
        self._catalog = catalog
        self._interpreter = interpreter
        self._prepare = prepare
        self._normalizers: dict[tuple[str, int], _NormalizationBinding] = {}

    def register(
        self, spec: CommandSpec[Any, Any],
        normalize: Callable[[Mapping[str, object], Actor], CandidateNormalization],
    ) -> None:
        if not isinstance(spec, CommandSpec):
            raise TypeError("Для регистрации нормализации требуется CommandSpec.")
        if spec.effect != CommandEffect.WRITE:
            raise ValueError("Подготовка черновика поддерживает только команды WRITE.")
        if not callable(normalize):
            raise TypeError("Нормализатор должен быть вызываемым объектом.")
        key = spec.name, spec.version
        if key in self._normalizers:
            raise ValueError(f"Нормализация {spec.key} уже зарегистрирована.")
        self._normalizers[key] = _NormalizationBinding(spec, normalize)

    def prepare_from_text(self, text: str, actor: Actor, *, lifetime: timedelta) -> TextPreparationResult:
        if not isinstance(lifetime, timedelta):
            raise TypeError("Срок действия черновика должен быть timedelta.")
        if lifetime <= timedelta(0):
            raise ValueError("Срок действия черновика должен быть положительным.")
        extraction = self._interpreter.interpret(text, actor)
        candidate = extraction.candidate
        if candidate is None:
            return TextPreparationResult(extraction)
        spec = self._catalog.get(candidate.command_name, candidate.command_version, actor)
        if spec.effect != CommandEffect.WRITE:
            raise ValueError("Подготовка черновика поддерживает только команды WRITE.")
        binding = self._normalizers.get((spec.name, spec.version))
        if binding is not None and binding.spec is not spec:
            raise ValueError("Нормализация зарегистрирована для другой спецификации команды.")
        parameters = deepcopy(candidate.parameters)
        normalized = binding.normalize(parameters, actor) if binding is not None else CandidateNormalization(parameters)
        if not isinstance(normalized, CandidateNormalization):
            raise TypeError("Нормализатор должен вернуть CandidateNormalization.")
        if normalized.resolution is not None and not isinstance(normalized.resolution, CommandModel):
            raise TypeError("Результат разрешения должен быть CommandModel.")
        draft = self._prepare(spec.name, spec.version, normalized.parameters, actor, lifetime=lifetime)
        return TextPreparationResult(extraction, draft, deepcopy(normalized.resolution))
