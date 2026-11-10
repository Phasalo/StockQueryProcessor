from __future__ import annotations

from typing import TYPE_CHECKING

from semantic_entry.adapters.postgres.transaction import PostgresCommandTransaction
from semantic_entry.core import (
    CommandCatalog,
    CommandPreparer,
    CommandRegistry,
    QueryExecutor,
)
from semantic_entry.core.engine import Engine
from semantic_entry.core.memory_drafts import MemoryDraftStore
from semantic_entry.modules.warehouse.commands.receipts import (
    RECORD_RECEIPT_SPEC,
    ReceiptPreview,
)
from semantic_entry.modules.warehouse.postgres.handlers.receipt_execution import (
    RecordReceiptHandler,
)
from semantic_entry.modules.warehouse.postgres.handlers.receipt_preview import (
    ReceiptPreviewHandler,
)
from semantic_entry.modules.warehouse.postgres.handlers.stock import StockQueryHandler
from semantic_entry.modules.warehouse.postgres.unit_of_work import UnitOfWork
from semantic_entry.modules.warehouse.queries.stock import GET_STOCK_SPEC

if TYPE_CHECKING:
    from config import WarehouseConfig
    from semantic_entry.core.ports.authorization import Authorizer
    from semantic_entry.core.ports.execution import CommandTransaction


def _receipt_handler(transaction: CommandTransaction) -> RecordReceiptHandler:
    if not isinstance(transaction, PostgresCommandTransaction):
        raise TypeError("Складской обработчик требует PostgreSQL-транзакцию.")
    return RecordReceiptHandler(transaction.connection)


def build_warehouse_engine(
    config: WarehouseConfig, authorizer: Authorizer, *, drafts: MemoryDraftStore | None = None,
) -> Engine:
    registry = CommandRegistry()
    registry.register(RECORD_RECEIPT_SPEC)
    registry.register(GET_STOCK_SPEC)
    catalog = CommandCatalog(registry, authorizer)
    preparer = CommandPreparer(catalog)
    preparer.register_preview(RECORD_RECEIPT_SPEC, ReceiptPreview, ReceiptPreviewHandler(config))
    queries = QueryExecutor(catalog)
    queries.register(GET_STOCK_SPEC, StockQueryHandler(config))
    engine = Engine(
        catalog, preparer, queries, drafts if drafts is not None else MemoryDraftStore(),
        lambda: PostgresCommandTransaction(UnitOfWork(config)),
    )
    engine.register_write(RECORD_RECEIPT_SPEC, ReceiptPreview, _receipt_handler)
    return engine
