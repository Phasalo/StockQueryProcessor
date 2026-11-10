from os import getenv
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from semantic_entry.adapters.postgres.config import PostgresConfig

TEMP_PATH = Path("temp")

load_dotenv(find_dotenv(), encoding="utf-8")

warehouse_config = PostgresConfig(
    database="warehouse",
    password=getenv("POSTGRESQL_PASSWORD"),
)
