from __future__ import annotations

from typing import TYPE_CHECKING

from semantic_entry.modules.warehouse.models import MaterialRecord, SupplierRecord
from semantic_entry.modules.warehouse.postgres.repositories.catalog import (
    FilmTypeRepository,
    MaterialRepository,
    SupplierRepository,
)
from semantic_entry.modules.warehouse.queries.receipt_resolution import (
    ReferenceResolution,
    ResolutionStatus,
    ResolveReceiptResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg2.extensions import connection

    from semantic_entry.adapters.postgres.unit_of_work import UnitOfWork
    from semantic_entry.core.access import Actor
    from semantic_entry.modules.warehouse.queries.receipt_resolution import (
        MaterialLookup,
        ResolveReceiptInput,
    )


class ReceiptResolutionHandler:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]):
        self._uow_factory = uow_factory

    def __call__(self, parameters: ResolveReceiptInput, actor: Actor) -> ResolveReceiptResult:
        if not parameters.material.has_criteria and parameters.supplier_name is None:
            return ResolveReceiptResult()
        with self._uow_factory() as uow:
            uow.connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
            return ResolveReceiptResult(
                material=self._material(uow.connection, parameters.material, parameters.limit),
                supplier=self._supplier(uow.connection, parameters.supplier_name, parameters.limit),
            )

    @staticmethod
    def _material(connection: connection, lookup: MaterialLookup, limit: int) -> ReferenceResolution[MaterialRecord]:
        if not lookup.has_criteria:
            return ReferenceResolution[MaterialRecord]()
        film_type_id = None
        if lookup.film_type_code is not None:
            film_type = FilmTypeRepository(connection).find_by_code(lookup.film_type_code)
            if film_type is None:
                return ReferenceResolution[MaterialRecord](status=ResolutionStatus.NOT_FOUND)
            film_type_id = film_type.id
        candidates = MaterialRepository(connection).search(
            film_type_id, lookup.brand, lookup.format, lookup.thickness,
            limit=limit + 1, exact_brand=True,
        )
        return ReferenceResolution[MaterialRecord].from_candidates(candidates, limit=limit)

    @staticmethod
    def _supplier(connection: connection, name: str | None, limit: int) -> ReferenceResolution[SupplierRecord]:
        if name is None:
            return ReferenceResolution[SupplierRecord]()
        repository = SupplierRepository(connection)
        exact = repository.find(name)
        candidates = [exact] if exact is not None else repository.search(name, limit=limit + 1, literal=True)
        return ReferenceResolution[SupplierRecord].from_candidates(candidates, limit=limit)
