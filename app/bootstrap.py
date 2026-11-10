from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from semantic_entry.adapters.memory.drafts import MemoryDraftStore
from semantic_entry.adapters.postgres.unit_of_work import UnitOfWork
from semantic_entry.modules.warehouse.bootstrap import build_warehouse_engine
from semantic_entry.modules.warehouse.reports import WarehouseReports

if TYPE_CHECKING:
    from semantic_entry.adapters.postgres.config import PostgresConfig
    from semantic_entry.core.engine import Engine
    from semantic_entry.core.ports.authorization import Authorizer
    from semantic_entry.core.ports.drafts import DraftStore


def build_reports(config: PostgresConfig) -> WarehouseReports:
    return WarehouseReports(partial(UnitOfWork, config))


def build_engine(
    config: PostgresConfig, authorizer: Authorizer, *, drafts: DraftStore | None = None,
) -> Engine:
    return build_warehouse_engine(
        partial(UnitOfWork, config), authorizer,
        drafts=drafts if drafts is not None else MemoryDraftStore(),
    )
