from __future__ import annotations

from typing import TYPE_CHECKING

from semantic_entry.modules.warehouse.postgres.repositories.reports import (
    ReportRepository,
)
from semantic_entry.modules.warehouse.postgres.unit_of_work import UnitOfWork
from semantic_entry.modules.warehouse.queries import GetStockResult

if TYPE_CHECKING:
    from config import WarehouseConfig
    from semantic_entry.core import Actor
    from semantic_entry.modules.warehouse.queries import GetStockInput


class StockQueryHandler:
    def __init__(self, config: WarehouseConfig):
        self._config = config

    def __call__(self, parameters: GetStockInput, actor: Actor) -> GetStockResult:
        with UnitOfWork(self._config) as uow:
            stock = ReportRepository(uow.connection).get_stock(parameters.material_id)
            return GetStockResult(stock=stock)
