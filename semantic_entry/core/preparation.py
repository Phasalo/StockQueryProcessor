from collections.abc import Mapping

from pydantic import ValidationError

from semantic_entry.core.catalog import CommandCatalog
from semantic_entry.core.contracts import Actor, FieldError, PreparationResult


class CommandPreparer:
    def __init__(self, catalog: CommandCatalog):
        self._catalog = catalog

    def prepare(
        self, name: str, version: int, payload: object, actor: Actor,
    ) -> PreparationResult:
        spec = self._catalog.get(name, version, actor)
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
        return PreparationResult(spec.name, spec.version, actor.actor_id, validated)
