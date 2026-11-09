from dataclasses import dataclass
from os import getenv
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

TEMP_PATH = Path("temp")

load_dotenv(find_dotenv(), encoding="utf-8")


@dataclass
class WarehouseConfig:
    database: str = "warehouse"
    password: str = getenv("POSTGRESQL_PASSWORD")
    user: str = "postgres"
    host: str = "localhost"
    port: int = 5432


warehouse_config = WarehouseConfig()
