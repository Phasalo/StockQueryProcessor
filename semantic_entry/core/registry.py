from typing import Any

from semantic_entry.core.contracts import CommandSpec


class CommandRegistry:
    def __init__(self):
        self._commands: dict[tuple[str, int], CommandSpec[Any, Any]] = {}

    def register(self, spec: CommandSpec[Any, Any]) -> None:
        if not isinstance(spec, CommandSpec):
            raise TypeError("В реестр можно добавить только CommandSpec.")
        key = spec.name, spec.version
        if key in self._commands:
            raise ValueError(f"Команда {spec.key} уже зарегистрирована.")
        self._commands[key] = spec

    def get(self, name: str, version: int) -> CommandSpec[Any, Any]:
        if type(version) is not int or version < 1:
            raise ValueError("Версия команды должна быть положительным целым числом.")
        return self._commands[name, version]

    def list(self) -> tuple[CommandSpec[Any, Any], ...]:
        return tuple(self._commands.values())
