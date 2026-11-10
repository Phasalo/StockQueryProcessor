from typing import Any

from semantic_entry.core.access import Actor, PermissionDeniedError
from semantic_entry.core.commands.models import CommandSpec
from semantic_entry.core.commands.registry import CommandRegistry
from semantic_entry.core.ports.authorization import Authorizer


class CommandCatalog:
    def __init__(self, registry: CommandRegistry, authorizer: Authorizer):
        self._registry = registry
        self._authorizer = authorizer

    def list(self, actor: Actor) -> tuple[CommandSpec[Any, Any], ...]:
        return tuple(
            spec for spec in self._registry.list()
            if self._authorizer.is_allowed(actor, spec.permission) is True
        )

    def get(self, name: str, version: int, actor: Actor) -> CommandSpec[Any, Any]:
        spec = self._registry.get(name, version)
        if self._authorizer.is_allowed(actor, spec.permission) is not True:
            raise PermissionDeniedError("Недостаточно прав для доступа к команде.")
        return spec
