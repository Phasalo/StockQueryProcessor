from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg2

if TYPE_CHECKING:
    from psycopg2.extensions import connection

    from semantic_entry.adapters.postgres.config import PostgresConfig


def create_connection(config: PostgresConfig) -> connection:
    return psycopg2.connect(
        dbname=config.database,
        user=config.user,
        password=config.password,
        host=config.host,
        port=config.port,
    )
