from __future__ import annotations

from typing import TYPE_CHECKING

from semantic_entry.modules.warehouse.postgres.repositories.reports import (
    ReportRepository,
)
from semantic_entry.modules.warehouse.queries.stock import GetStockResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from semantic_entry.adapters.postgres.unit_of_work import UnitOfWork
    from semantic_entry.core.access import Actor
    from semantic_entry.modules.warehouse.queries.stock import GetStockInput


class StockQueryHandler:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]):
        self._uow_factory = uow_factory

    def __call__(self, parameters: GetStockInput, actor: Actor) -> GetStockResult:
        with self._uow_factory() as uow:
            stock = ReportRepository(uow.connection).get_stock(parameters.material_id)
            return GetStockResult(stock=stock)
