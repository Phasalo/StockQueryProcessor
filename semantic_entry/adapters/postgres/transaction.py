from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

from semantic_entry.adapters.postgres.executions import PostgresExecutionStore

if TYPE_CHECKING:
    from types import TracebackType

    from psycopg2.extensions import connection


class ConnectionUnitOfWork(Protocol):
    @property
    def connection(self) -> connection: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None: ...


class PostgresCommandTransaction:
    def __init__(self, uow: ConnectionUnitOfWork):
        self._uow = uow
        self._executions: PostgresExecutionStore | None = None

    @property
    def connection(self) -> connection:
        return self._uow.connection

    @property
    def executions(self) -> PostgresExecutionStore:
        if self._executions is None:
            raise RuntimeError("Транзакция доступна только внутри with.")
        return self._executions

    def __enter__(self) -> Self:
        self._uow.__enter__()
        self._executions = PostgresExecutionStore(self.connection)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        try:
            self._uow.__exit__(exc_type, exc_value, traceback)
        finally:
            self._executions = None
