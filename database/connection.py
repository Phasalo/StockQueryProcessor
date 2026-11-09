import psycopg2
from psycopg2.extensions import connection

from config import WarehouseConfig


def create_connection(config: WarehouseConfig) -> connection:
    return psycopg2.connect(
        dbname=config.database,
        user=config.user,
        password=config.password,
        host=config.host,
        port=config.port,
    )
