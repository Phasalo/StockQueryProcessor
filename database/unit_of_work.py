from __future__ import annotations

from typing import TYPE_CHECKING

from database.connection import create_connection

if TYPE_CHECKING:
    from types import TracebackType

    from psycopg2.extensions import connection

    from config import WarehouseConfig


class UnitOfWork:
    def __init__(self, config: WarehouseConfig):
        self._config = config
        self._connection: connection | None = None

    @property
    def connection(self) -> connection:
        if self._connection is None:
            raise RuntimeError("UnitOfWork доступен только внутри with.")
        return self._connection

    def __enter__(self) -> UnitOfWork:
        if self._connection is not None:
            raise RuntimeError("UnitOfWork уже используется.")
        self._connection = create_connection(self._config)
        return self

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self.connection
        self._connection = None
        try:
            if exc_type is not None:
                connection.rollback()
            else:
                try:
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        finally:
            connection.close()
