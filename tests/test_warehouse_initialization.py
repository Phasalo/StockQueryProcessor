import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from app.bootstrap import build_engine, build_reports
from semantic_entry.adapters.memory.drafts import MemoryDraftStore
from semantic_entry.adapters.postgres.config import PostgresConfig
from semantic_entry.core.access import Actor
from semantic_entry.modules.warehouse.bootstrap import build_warehouse_engine
from semantic_entry.modules.warehouse.reports import WarehouseReports
from semantic_entry.modules.warehouse.service import Warehouse


class WarehouseInitializationTest(unittest.TestCase):
    def test_constructor_does_not_connect_or_create_schema(self):
        factory = Mock(side_effect=AssertionError("Unexpected transaction"))
        Warehouse(factory)
        WarehouseReports(factory)
        build_warehouse_engine(factory, Mock(), drafts=MemoryDraftStore())
        factory.assert_not_called()

    def test_module_uses_injected_draft_store_without_opening_transaction(self):
        factory = Mock(side_effect=AssertionError("Unexpected transaction"))
        drafts = MemoryDraftStore()
        actor = Actor("alice")
        authorizer = Mock()
        authorizer.is_allowed.return_value = True
        engine = build_warehouse_engine(factory, authorizer, drafts=drafts)
        draft = engine.prepare("warehouse.record_receipt", 1, {}, actor, lifetime=timedelta(minutes=5))
        self.assertEqual(drafts.get(draft.draft_id, actor), draft)
        factory.assert_not_called()

    def test_application_assembly_does_not_connect(self):
        config = PostgresConfig(database="warehouse")
        with patch("semantic_entry.adapters.postgres.unit_of_work.create_connection") as connect:
            build_reports(config)
            build_engine(config, Mock())
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
