from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from importlib.resources import files
from math import isfinite
from typing import TYPE_CHECKING, NoReturn

from semantic_entry.core.commands.extraction import (
    ExtractionContractError,
    ExtractionResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from semantic_entry.core.commands.extraction import ExtractionCommand



def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON key")
        result[name] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError("Non-finite JSON number")


def _parse_float(value: str) -> float:
    number = float(value)
    if not isfinite(number) or Decimal(value) != Decimal(str(number)):
        raise ValueError("JSON number cannot be represented without loss of precision")
    return number


class JsonCommandExtractor:
    def __init__(self, complete: Callable[[str, str], str]):
        if not callable(complete):
            raise TypeError("Для извлечения требуется вызываемый клиент LLM.")
        self._complete = complete
        self._instructions = files("semantic_entry.adapters.llm").joinpath("prompts", "extraction.md").read_text(encoding="utf-8")

    def extract(self, text: str, commands: tuple[ExtractionCommand, ...]) -> ExtractionResult:
        if not isinstance(text, str):
            raise TypeError("Для извлечения требуется текст.")
        if not text.strip():
            raise ValueError("Текст не может быть пустым.")
        if not commands:
            raise ValueError("Каталог извлечения не может быть пустым.")
        catalog = json.dumps({
            "commands": [asdict(command) for command in commands],
            "response_schema": ExtractionResult.model_json_schema(),
        }, ensure_ascii=False, allow_nan=False)
        response = self._complete(self._instructions.rstrip() + "\n" + catalog, text)
        if not isinstance(response, str):
            raise ExtractionContractError("Клиент LLM должен вернуть текст JSON-ответа.")
        try:
            payload = json.loads(
                response, object_pairs_hook=_unique_object,
                parse_constant=_reject_constant, parse_float=_parse_float,
            )
            return ExtractionResult.model_validate(payload)
        except (ValueError, TypeError) as error:
            raise ExtractionContractError("LLM вернул некорректный JSON извлечения.") from error
