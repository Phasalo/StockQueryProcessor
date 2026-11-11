from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from semantic_entry.core.commands.extraction import (
        ExtractionCommand,
        ExtractionResult,
    )


class CommandExtractor(Protocol):
    def extract(self, text: str, commands: tuple[ExtractionCommand, ...]) -> ExtractionResult: ...
