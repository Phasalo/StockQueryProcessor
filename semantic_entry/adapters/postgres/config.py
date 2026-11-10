from dataclasses import dataclass


@dataclass(frozen=True)
class PostgresConfig:
    database: str
    password: str | None = None
    user: str = "postgres"
    host: str = "localhost"
    port: int = 5432
