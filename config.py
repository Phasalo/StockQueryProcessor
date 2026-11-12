from os import getenv
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from semantic_entry.adapters.llm.yandex import YandexLlmConfig
from semantic_entry.adapters.postgres.config import PostgresConfig

TEMP_PATH = Path("temp")

load_dotenv(find_dotenv(), encoding="utf-8")

warehouse_config = PostgresConfig(
    database="warehouse",
    password=getenv("POSTGRESQL_PASSWORD"),
)


def load_yandex_config() -> YandexLlmConfig:
    api_key = getenv("YANDEX_API_KEY", "")
    model_uri = getenv("YANDEX_MODEL_URI", "")
    missing = [name for name, value in (("YANDEX_API_KEY", api_key), ("YANDEX_MODEL_URI", model_uri)) if not value]
    if missing:
        raise ValueError("Задайте переменные окружения: " + ", ".join(missing))
    return YandexLlmConfig(model_uri=model_uri, api_key=api_key)
