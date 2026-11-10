from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from psycopg2.extras import Json, RealDictCursor

from semantic_entry.core.executions.models import ExecutionRecord

if TYPE_CHECKING:
    from psycopg2.extensions import connection


class PostgresExecutionStore:
    def __init__(self, connection: connection):
        self._connection = connection

    def lock(self, actor_id: str, key: str, draft_id: UUID) -> None:
        resources = (
            json.dumps(["idempotency", actor_id, key], ensure_ascii=False),
            json.dumps(["execution", str(draft_id)]),
        )
        with self._connection.cursor() as cursor:
            for resource in resources:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (resource,))

    def get_by_key(self, actor_id: str, key: str) -> ExecutionRecord | None:
        return self._fetch(
            "SELECT e.* FROM engine.executions e "
            "JOIN engine.idempotency_records i ON i.execution_id = e.execution_id "
            "WHERE i.actor_id = %s AND i.key = %s",
            (actor_id, key),
        )

    def get_by_draft(self, draft_id: UUID) -> ExecutionRecord | None:
        return self._fetch("SELECT * FROM engine.executions WHERE draft_id = %s", (str(draft_id),))

    def add(self, execution: ExecutionRecord, key: str) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO engine.executions "
                "(execution_id, draft_id, draft_version, actor_id, command_name, command_version, request_hash, request, result) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(execution.execution_id), str(execution.draft_id), execution.draft_version,
                    execution.actor_id, execution.command_name, execution.command_version,
                    execution.request_hash, Json(execution.request), Json(execution.result),
                ),
            )
            self.bind_key(execution, key)
            cursor.execute(
                "INSERT INTO engine.audit_events (execution_id, action) VALUES (%s, %s)",
                (str(execution.execution_id), "command.executed"),
            )

    def bind_key(self, execution: ExecutionRecord, key: str) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO engine.idempotency_records (actor_id, key, execution_id) VALUES (%s, %s, %s)",
                (execution.actor_id, key, str(execution.execution_id)),
            )

    def _fetch(self, query: str, params: tuple) -> ExecutionRecord | None:
        with self._connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(query, params)
            row = cursor.fetchone()
        if row is None:
            return None
        return ExecutionRecord(
            execution_id=UUID(str(row["execution_id"])), draft_id=UUID(str(row["draft_id"])),
            draft_version=row["draft_version"], actor_id=row["actor_id"],
            command_name=row["command_name"], command_version=row["command_version"],
            request_hash=row["request_hash"], request=row["request"], result=row["result"],
        )
