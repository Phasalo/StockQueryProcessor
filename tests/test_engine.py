import hashlib
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from os import environ, getenv
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock, patch
from uuid import uuid4

from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from pydantic import StrictInt, ValidationError

from config import WarehouseConfig
from semantic_entry.adapters.postgres.executions import PostgresExecutionStore
from semantic_entry.adapters.postgres.transaction import PostgresCommandTransaction
from semantic_entry.core import (
    Actor,
    CommandCatalog,
    CommandEffect,
    CommandModel,
    CommandPreparer,
    CommandRegistry,
    CommandSpec,
    PermissionDeniedError,
    QueryExecutor,
)
from semantic_entry.core.contracts import PreviewResult
from semantic_entry.core.drafts import (
    DraftClosedError,
    DraftExpiredError,
    DraftNotFoundError,
    DraftNotReadyError,
    DraftPreviewChangedError,
    DraftState,
    DraftVersionConflictError,
)
from semantic_entry.core.engine import Engine
from semantic_entry.core.executions import IdempotencyConflictError
from semantic_entry.core.memory_drafts import MemoryDraftStore
from semantic_entry.modules.warehouse.bootstrap import build_warehouse_engine
from semantic_entry.modules.warehouse.commands.receipts import RecordReceiptResult
from semantic_entry.modules.warehouse.postgres.connection import create_connection
from semantic_entry.modules.warehouse.postgres.handlers.receipt_execution import (
    RecordReceiptHandler,
)
from semantic_entry.modules.warehouse.postgres.repositories.catalog import (
    FilmTypeRepository,
    MaterialRepository,
    SupplierRepository,
)
from semantic_entry.modules.warehouse.postgres.repositories.plans import PlanRepository
from semantic_entry.modules.warehouse.postgres.unit_of_work import UnitOfWork

ROOT = Path(__file__).resolve().parent.parent


class NoteInput(CommandModel):
    title: str


class NotePreview(CommandModel):
    title: str


class NoteResult(CommandModel):
    note_id: StrictInt


class NoteHandler:
    def __init__(self, connection):
        self.connection = connection

    def preview(self, parameters, actor):
        return PreviewResult[NotePreview](preview=NotePreview(title=parameters.title))

    def execute(self, parameters, actor):
        with self.connection.cursor() as cursor:
            cursor.execute("INSERT INTO engine.test_notes (title) VALUES (%s) RETURNING id", (parameters.title,))
            return NoteResult(note_id=cursor.fetchone()[0])


class EnginePostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = getenv("TEST_DATABASE_DSN")
        if not dsn:
            raise unittest.SkipTest("Set TEST_DATABASE_DSN to run PostgreSQL tests")
        settings = parse_dsn(dsn)
        cls.admin_config = WarehouseConfig(
            database=settings["dbname"], user=settings["user"], password=settings.get("password", ""),
            host=settings["host"], port=int(settings.get("port", 5432)),
        )
        cls.config = replace(cls.admin_config, database="engine_test_" + uuid4().hex)
        with closing(create_connection(cls.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.config.database)))
        cls.addClassCleanup(cls.drop_database)
        env = environ.copy()
        env.update(PGDATABASE=cls.config.database, PGUSER=cls.config.user, PGPASSWORD=cls.config.password, PGHOST=cls.config.host, PGPORT=str(cls.config.port))
        result = subprocess.run(
            [sys.executable, "-m", "pgmigrate", "-d", str(ROOT), "-t", "latest", "--check_serial_versions", "migrate"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        with UnitOfWork(cls.config) as uow:
            film_type = FilmTypeRepository(uow.connection).find_by_code("BOPP")
            cls.material_id = MaterialRepository(uow.connection).add(film_type.id, "Brand", 1200, 25)
            cls.supplier_id = SupplierRepository(uow.connection).add("Supplier")
            with uow.connection.cursor() as cursor:
                cursor.execute("CREATE TABLE engine.test_notes (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, title text NOT NULL)")

    @classmethod
    def drop_database(cls):
        with closing(create_connection(cls.admin_config)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.config.database)))

    def setUp(self):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute("TRUNCATE transactions RESTART IDENTITY CASCADE")
                cursor.execute("TRUNCATE engine.executions, engine.test_notes RESTART IDENTITY CASCADE")
                cursor.execute("UPDATE suppliers SET name = 'Supplier' WHERE id = %s", (self.supplier_id,))
        self.actor = Actor("alice")
        self.policy = Mock()
        self.policy.is_allowed.return_value = True
        self.now = datetime(2026, 11, 10, 12, tzinfo=UTC)
        self.clock = Mock(return_value=self.now)
        self.drafts = MemoryDraftStore(self.clock)
        self.engine = build_warehouse_engine(self.config, self.policy, drafts=self.drafts)

    def query(self, statement, parameters=()):
        with UnitOfWork(self.config) as uow:
            with uow.connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                return cursor.fetchall()

    def counts(self):
        return tuple(self.query(f"SELECT count(*) FROM {table}")[0][0] for table in (
            "receipts", "engine.executions", "engine.idempotency_records", "engine.audit_events",
        ))

    def prepare(self, *, actor=None, **changes):
        payload = {"material_id": self.material_id, "supplier_id": self.supplier_id, "actual_date": "2026-11-10", "quantity_kg": "12.50", **changes}
        return self.engine.prepare("warehouse.record_receipt", 1, payload, actor or self.actor, lifetime=timedelta(minutes=10))

    def confirm(self, draft, key="request-1", *, actor=None, engine=None):
        return (engine or self.engine).confirm(draft.draft_id, draft.version, key, actor or self.actor)

    def plan(self):
        with UnitOfWork(self.config) as uow:
            return PlanRepository(uow.connection).add_receipt(self.material_id, self.supplier_id, date(2026, 11, 1), date(2026, 11, 10), Decimal("100"))

    def test_complete_receipt_flow_and_durable_request_audit(self):
        draft = self.prepare()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.query("SELECT count(*) FROM transactions"), [(0,)])
        revised = self.engine.revise(draft.draft_id, 1, {"quantity_kg": "15.50"}, self.actor)
        result = self.confirm(revised)
        self.assertIsInstance(result, RecordReceiptResult)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        executed = self.engine.get_draft(draft.draft_id, self.actor)
        self.assertEqual((executed.version, executed.state), (2, DraftState.EXECUTED))
        stock = self.engine.query("warehouse.get_stock", 1, {"material_id": self.material_id}, self.actor).stock
        self.assertEqual(stock.on_hand_kg, Decimal("15.50"))
        self.assertEqual(stock.available_kg, Decimal("15.50"))
        request, signature, saved_result = self.query("SELECT request, request_hash, result FROM engine.executions")[0]
        self.assertEqual(request["parameters"]["quantity_kg"], "15.50")
        self.assertEqual(request["draft_version"], 2)
        self.assertEqual(request["actor_id"], self.actor.actor_id)
        self.assertEqual(saved_result, result.model_dump(mode="json"))
        self.assertEqual(signature, hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest())
        with self.assertRaises(DraftClosedError):
            self.engine.cancel(draft.draft_id, 2, self.actor)

    def test_partial_draft_is_completed_before_confirmation(self):
        draft = self.engine.prepare("warehouse.record_receipt", 1, {}, self.actor, lifetime=timedelta(minutes=10))
        with self.assertRaises(DraftNotReadyError):
            self.confirm(draft)
        revised = self.engine.revise(draft.draft_id, 1, {"material_id": self.material_id, "supplier_id": self.supplier_id, "actual_date": "2026-11-10", "quantity_kg": "1.25"}, self.actor)
        self.confirm(revised)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_cancelled_expired_and_stale_drafts_are_not_executed(self):
        cancelled = self.prepare()
        self.engine.cancel(cancelled.draft_id, 1, self.actor)
        with self.assertRaises(DraftClosedError):
            self.engine.confirm(cancelled.draft_id, 2, "cancelled", self.actor)
        stale = self.prepare()
        self.engine.revise(stale.draft_id, 1, {"quantity_kg": "1"}, self.actor)
        with self.assertRaises(DraftVersionConflictError):
            self.confirm(stale, "stale")
        expired = self.prepare()
        self.clock.return_value = expired.expires_at
        with self.assertRaises(DraftExpiredError):
            self.confirm(expired, "expired")
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_author_and_current_permission_are_checked(self):
        draft = self.prepare()
        with self.assertRaises(PermissionDeniedError):
            self.confirm(draft, actor=Actor("bob"))
        self.policy.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.confirm(draft)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.engine.get_draft(draft.draft_id, self.actor).state, DraftState.READY)

    def test_repeat_and_new_key_return_one_execution(self):
        draft = self.prepare()
        result = self.confirm(draft)
        self.assertEqual(self.confirm(draft), result)
        self.assertEqual(self.confirm(draft, "another-key"), result)
        self.assertEqual(self.counts(), (1, 1, 2, 1))
        self.clock.return_value = draft.expires_at
        self.assertEqual(self.confirm(draft), result)

    def test_same_key_for_another_request_conflicts(self):
        first = self.prepare()
        self.confirm(first)
        second = self.prepare(quantity_kg="20")
        with self.assertRaises(IdempotencyConflictError):
            self.confirm(second)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(self.engine.get_draft(second.draft_id, self.actor).state, DraftState.READY)
        self.confirm(second, "second-request")
        self.assertEqual(self.counts(), (2, 2, 2, 2))

    def test_idempotency_keys_are_scoped_to_actor(self):
        self.confirm(self.prepare())
        bob = Actor("bob")
        self.confirm(self.prepare(actor=bob), actor=bob)
        self.assertEqual(self.counts(), (2, 2, 2, 2))

    def test_committed_result_is_recovered_after_loss_of_memory(self):
        draft = self.prepare()
        result = self.confirm(draft)
        restarted = build_warehouse_engine(self.config, self.policy)
        self.assertEqual(self.confirm(draft, engine=restarted), result)
        self.assertEqual(self.confirm(draft, "after-restart", engine=restarted), result)
        self.assertEqual(self.counts(), (1, 1, 2, 1))
        with self.assertRaises(DraftNotFoundError):
            restarted.get_draft(draft.draft_id, self.actor)

    def test_unconfirmed_lost_draft_is_not_reconstructed_from_request(self):
        draft = self.prepare()
        restarted = build_warehouse_engine(self.config, self.policy)
        with self.assertRaises(DraftNotFoundError):
            self.confirm(draft, engine=restarted)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_recovered_result_still_requires_author_and_permission(self):
        draft = self.prepare()
        self.confirm(draft)
        restarted = build_warehouse_engine(self.config, self.policy)
        with self.assertRaises(PermissionDeniedError):
            self.confirm(draft, "other-author", actor=Actor("bob"), engine=restarted)
        self.policy.is_allowed.return_value = False
        with self.assertRaises(PermissionDeniedError):
            self.confirm(draft, "revoked", engine=restarted)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_changed_preview_requires_new_confirmation(self):
        draft = self.prepare()
        self.query("UPDATE suppliers SET name = 'Renamed' WHERE id = %s RETURNING id", (self.supplier_id,))
        with self.assertRaises(DraftPreviewChangedError) as caught:
            self.confirm(draft)
        updated = caught.exception.draft
        self.assertEqual((updated.version, updated.state), (2, DraftState.READY))
        self.assertEqual(updated.preparation.preview.supplier_name, "Renamed")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        with self.assertRaises(DraftVersionConflictError):
            self.confirm(draft)
        self.confirm(updated)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_closed_plan_replaces_preview_with_errors(self):
        plan = self.plan()
        draft = self.prepare(planned_receipt_id=plan.id)
        with UnitOfWork(self.config) as uow:
            PlanRepository(uow.connection).cancel(plan.transaction_id, Decimal("100"), "Closed")
        with self.assertRaises(DraftPreviewChangedError) as caught:
            self.confirm(draft)
        updated = caught.exception.draft
        self.assertEqual((updated.version, updated.state), (2, DraftState.NEEDS_INPUT))
        self.assertEqual(updated.preparation.errors[0].code, "plan_closed")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        fixed = self.engine.revise(draft.draft_id, 2, {"planned_receipt_id": None}, self.actor)
        self.confirm(fixed)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_partial_cancellation_refreshes_preview_without_limiting_fact(self):
        plan = self.plan()
        draft = self.prepare(planned_receipt_id=plan.id, quantity_kg="150")
        with UnitOfWork(self.config) as uow:
            PlanRepository(uow.connection).cancel(plan.transaction_id, Decimal("25"), "Partial")
        with self.assertRaises(DraftPreviewChangedError) as caught:
            self.confirm(draft)
        updated = caught.exception.draft
        self.assertEqual(updated.preparation.preview.plan_remaining_quantity_kg, Decimal("75"))
        self.confirm(updated)
        with UnitOfWork(self.config) as uow:
            self.assertTrue(PlanRepository(uow.connection).get_receipt(plan.id).is_closed)
        self.assertEqual(self.engine.query("warehouse.get_stock", 1, {"material_id": self.material_id}, self.actor).stock.on_hand_kg, Decimal("150"))

    def test_failure_after_ledger_writes_rolls_back_everything(self):
        draft = self.prepare()
        original = PostgresExecutionStore.add

        def fail(store, execution, key):
            original(store, execution, key)
            raise RuntimeError("After audit")

        with patch.object(PostgresExecutionStore, "add", fail):
            with self.assertRaisesRegex(RuntimeError, "After audit"):
                self.confirm(draft)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.query("SELECT count(*) FROM transactions"), [(0,)])
        self.assertEqual(self.engine.get_draft(draft.draft_id, self.actor).state, DraftState.READY)
        self.confirm(draft)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_invalid_handler_result_rolls_back_movement(self):
        draft = self.prepare()
        original = RecordReceiptHandler.execute

        def invalid(handler, parameters, actor):
            result = original(handler, parameters, actor)
            result.receipt_id = 0
            return result

        with patch.object(RecordReceiptHandler, "execute", invalid):
            with self.assertRaises(ValidationError):
                self.confirm(draft)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.query("SELECT count(*) FROM transactions"), [(0,)])

    def test_commit_failure_does_not_mark_draft_executed(self):
        draft = self.prepare()
        original = PostgresCommandTransaction.__exit__

        def fail_commit(transaction, exc_type, exc_value, traceback):
            if exc_type is None:
                error = RuntimeError("Commit failed")
                original(transaction, type(error), error, None)
                raise error
            return original(transaction, exc_type, exc_value, traceback)

        with patch.object(PostgresCommandTransaction, "__exit__", fail_commit):
            with self.assertRaisesRegex(RuntimeError, "Commit failed"):
                self.confirm(draft)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.engine.get_draft(draft.draft_id, self.actor).state, DraftState.READY)
        self.confirm(draft)

    def test_memory_failure_after_commit_is_recovered_without_second_receipt(self):
        draft = self.prepare()
        with patch.object(self.drafts, "mark_executed", side_effect=RuntimeError("Memory failed")):
            with self.assertRaisesRegex(RuntimeError, "Memory failed"):
                self.confirm(draft)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        result = self.confirm(draft)
        self.assertIsInstance(result, RecordReceiptResult)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(self.engine.get_draft(draft.draft_id, self.actor).state, DraftState.EXECUTED)

    def test_durable_result_is_replayed_when_memory_update_failed_and_draft_expired(self):
        draft = self.prepare()
        with patch.object(self.drafts, "mark_executed", side_effect=RuntimeError("Memory failed")):
            with self.assertRaises(RuntimeError):
                self.confirm(draft)
        self.clock.return_value = draft.expires_at
        self.assertIsInstance(self.confirm(draft), RecordReceiptResult)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_committed_draft_cannot_be_edited_or_cancelled_after_memory_failure(self):
        for action in ("revise", "cancel"):
            with self.subTest(action=action):
                draft = self.prepare()
                key = f"memory-{action}"
                with patch.object(self.drafts, "mark_executed", side_effect=RuntimeError("Memory failed")):
                    with self.assertRaises(RuntimeError):
                        self.confirm(draft, key)
                with self.assertRaises(DraftClosedError):
                    if action == "revise":
                        self.engine.revise(draft.draft_id, 1, {"quantity_kg": "20"}, self.actor)
                    else:
                        self.engine.cancel(draft.draft_id, 1, self.actor)
                self.assertEqual(self.engine.get_draft(draft.draft_id, self.actor).state, DraftState.EXECUTED)
                self.assertIsInstance(self.confirm(draft, key), RecordReceiptResult)
        self.assertEqual(self.counts(), (2, 2, 2, 2))

    def test_invalid_confirmation_metadata_is_rejected_before_connection(self):
        draft = self.prepare()
        with patch("semantic_entry.modules.warehouse.postgres.unit_of_work.create_connection") as connect:
            for version in (True, 0, "1"):
                with self.subTest(version=version):
                    with self.assertRaises(ValueError):
                        self.engine.confirm(draft.draft_id, version, "request", self.actor)
            for key in (None, "", "   ", 1):
                with self.subTest(key=key):
                    with self.assertRaises(ValueError):
                        self.engine.confirm(draft.draft_id, 1, key, self.actor)
            connect.assert_not_called()

    def test_concurrent_repeat_creates_one_receipt(self):
        draft = self.prepare()
        barrier = Barrier(2)

        def confirm():
            barrier.wait(timeout=3)
            return self.confirm(draft)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(confirm) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_concurrent_different_drafts_with_same_key_conflict(self):
        drafts = (self.prepare(), self.prepare(quantity_kg="20"))
        barrier = Barrier(2)

        def confirm(draft):
            barrier.wait(timeout=3)
            try:
                return self.confirm(draft)
            except IdempotencyConflictError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(confirm, drafts))
        self.assertEqual(sum(isinstance(result, RecordReceiptResult) for result in results), 1)
        self.assertEqual(sum(isinstance(result, IdempotencyConflictError) for result in results), 1)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_concurrent_drafts_for_one_plan_create_one_fact(self):
        plan = self.plan()
        drafts = (self.prepare(planned_receipt_id=plan.id), self.prepare(planned_receipt_id=plan.id))
        barrier = Barrier(2)

        def confirm(draft):
            barrier.wait(timeout=3)
            try:
                return self.confirm(draft, str(draft.draft_id))
            except DraftPreviewChangedError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(confirm, drafts))
        self.assertEqual(sum(isinstance(result, RecordReceiptResult) for result in results), 1)
        conflicts = [result for result in results if isinstance(result, DraftPreviewChangedError)]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].draft.state, DraftState.NEEDS_INPUT)
        self.assertEqual(conflicts[0].draft.preparation.errors[0].code, "plan_closed")
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_cancellation_waits_for_confirmation_and_cannot_cancel_committed_receipt(self):
        draft = self.prepare()
        entered = Event()
        release = Event()
        cancel_started = Event()
        cancel_finished = Event()
        original = RecordReceiptHandler.execute

        def execute(handler, parameters, actor):
            result = original(handler, parameters, actor)
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Confirmation was not released")
            return result

        def cancel():
            cancel_started.set()
            try:
                return self.engine.cancel(draft.draft_id, 1, self.actor)
            finally:
                cancel_finished.set()

        with patch.object(RecordReceiptHandler, "execute", execute):
            with ThreadPoolExecutor(max_workers=2) as executor:
                confirmed = executor.submit(self.confirm, draft)
                try:
                    self.assertTrue(entered.wait(timeout=3))
                    cancelled = executor.submit(cancel)
                    self.assertTrue(cancel_started.wait(timeout=3))
                    self.assertFalse(cancel_finished.wait(timeout=0.05))
                    self.assertEqual(self.counts(), (0, 0, 0, 0))
                finally:
                    release.set()
                self.assertIsInstance(confirmed.result(timeout=5), RecordReceiptResult)
                with self.assertRaises(DraftClosedError):
                    cancelled.result(timeout=5)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(self.engine.get_draft(draft.draft_id, self.actor).state, DraftState.EXECUTED)

    def test_core_runs_a_nonwarehouse_module_in_same_transaction_contract(self):
        spec = CommandSpec("notes.create", 1, CommandEffect.WRITE, NoteInput, NoteResult, "Create note", "notes.write")
        registry = CommandRegistry()
        registry.register(spec)
        catalog = CommandCatalog(registry, self.policy)
        preparer = CommandPreparer(catalog)
        preparer.register_preview(spec, NotePreview, lambda parameters, actor: PreviewResult[NotePreview](preview=NotePreview(title=parameters.title)))
        engine = Engine(catalog, preparer, QueryExecutor(catalog), MemoryDraftStore(), lambda: PostgresCommandTransaction(UnitOfWork(self.config)))
        engine.register_write(spec, NotePreview, lambda transaction: NoteHandler(transaction.connection))
        draft = engine.prepare("notes.create", 1, {"title": "Independent"}, self.actor, lifetime=timedelta(minutes=10))
        result = engine.confirm(draft.draft_id, 1, "note", self.actor)
        self.assertIsInstance(result, NoteResult)
        self.assertEqual(self.query("SELECT title FROM engine.test_notes WHERE id = %s", (result.note_id,)), [("Independent",)])
        self.assertEqual(self.counts(), (0, 1, 1, 1))

    def test_bootstrap_does_not_connect_and_read_is_not_prepared_as_write(self):
        with patch("semantic_entry.modules.warehouse.postgres.unit_of_work.create_connection") as connect:
            engine = build_warehouse_engine(self.config, self.policy)
            with self.assertRaisesRegex(ValueError, "WRITE"):
                engine.prepare("warehouse.get_stock", 1, {"material_id": self.material_id}, self.actor, lifetime=timedelta(minutes=10))
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
