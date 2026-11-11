from __future__ import annotations

from typing import TYPE_CHECKING

from semantic_entry.adapters.postgres.transaction import PostgresCommandTransaction
from semantic_entry.core.commands.catalog import CommandCatalog
from semantic_entry.core.commands.preparation import CommandPreparer
from semantic_entry.core.commands.queries import QueryExecutor
from semantic_entry.core.commands.registry import CommandRegistry
from semantic_entry.core.engine import Engine
from semantic_entry.modules.warehouse.commands.receipt_intent import ReceiptIntent
from semantic_entry.modules.warehouse.commands.receipt_normalization import (
    ReceiptIntentNormalizer,
)
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
from semantic_entry.modules.warehouse.postgres.handlers.receipt_resolution import (
    ReceiptResolutionHandler,
)
from semantic_entry.modules.warehouse.postgres.handlers.stock import StockQueryHandler
from semantic_entry.modules.warehouse.queries.receipt_resolution import (
    RESOLVE_RECEIPT_SPEC,
)
from semantic_entry.modules.warehouse.queries.stock import GET_STOCK_SPEC

if TYPE_CHECKING:
    from collections.abc import Callable

    from semantic_entry.adapters.postgres.unit_of_work import UnitOfWork
    from semantic_entry.core.ports.authorization import Authorizer
    from semantic_entry.core.ports.drafts import DraftStore
    from semantic_entry.core.ports.execution import CommandTransaction
    from semantic_entry.core.ports.extraction import CommandExtractor


def _receipt_handler(transaction: CommandTransaction) -> RecordReceiptHandler:
    if not isinstance(transaction, PostgresCommandTransaction):
        raise TypeError("Складской обработчик требует PostgreSQL-транзакцию.")
    return RecordReceiptHandler(transaction.connection)


def build_warehouse_engine(
    uow_factory: Callable[[], UnitOfWork], authorizer: Authorizer, *, drafts: DraftStore,
    extractor: CommandExtractor | None = None,
) -> Engine:
    registry = CommandRegistry()
    registry.register(RECORD_RECEIPT_SPEC)
    registry.register(GET_STOCK_SPEC)
    registry.register(RESOLVE_RECEIPT_SPEC)
    catalog = CommandCatalog(registry, authorizer)
    preparer = CommandPreparer(catalog)
    preparer.register_preview(RECORD_RECEIPT_SPEC, ReceiptPreview, ReceiptPreviewHandler(uow_factory))
    queries = QueryExecutor(catalog)
    queries.register(GET_STOCK_SPEC, StockQueryHandler(uow_factory))
    queries.register(RESOLVE_RECEIPT_SPEC, ReceiptResolutionHandler(uow_factory))
    engine = Engine(
        catalog, preparer, queries, drafts,
        lambda: PostgresCommandTransaction(uow_factory()),
        extractor=extractor,
    )
    engine.register_write(RECORD_RECEIPT_SPEC, ReceiptPreview, _receipt_handler)
    engine.register_extraction(RECORD_RECEIPT_SPEC, ReceiptIntent)
    engine.register_normalizer(RECORD_RECEIPT_SPEC, ReceiptIntentNormalizer(ReceiptResolutionHandler(uow_factory)))
    return engine
