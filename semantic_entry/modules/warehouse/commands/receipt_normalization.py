from __future__ import annotations

from typing import TYPE_CHECKING

from semantic_entry.core.commands.text import CandidateNormalization
from semantic_entry.modules.warehouse.commands.receipt_intent import ReceiptIntent
from semantic_entry.modules.warehouse.queries.receipt_resolution import (
    ResolveReceiptInput,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from semantic_entry.core.access import Actor
    from semantic_entry.modules.warehouse.queries.receipt_resolution import (
        ResolveReceiptResult,
    )


class ReceiptIntentNormalizer:
    def __init__(self, resolve: Callable[[ResolveReceiptInput, Actor], ResolveReceiptResult]):
        self._resolve = resolve

    def __call__(self, parameters: Mapping[str, object], actor: Actor) -> CandidateNormalization:
        intent = ReceiptIntent.model_validate(parameters)
        resolution = self._resolve(ResolveReceiptInput(
            material=intent.material, supplier_name=intent.supplier_name,
        ), actor)
        payload: dict[str, object] = dict(resolution.resolved_ids)
        if intent.quantity_kg is not None:
            payload["quantity_kg"] = intent.quantity_kg
        if intent.actual_date is not None:
            payload["actual_date"] = intent.actual_date
        return CandidateNormalization(payload, resolution)
