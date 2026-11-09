import unittest
from unittest.mock import patch

from config import WarehouseConfig
from database import Warehouse


class WarehouseInitializationTest(unittest.TestCase):
    def test_constructor_does_not_connect_or_create_schema(self):
        with patch(
            "database.psycopg2.connect",
            side_effect=AssertionError("Warehouse must not connect during initialization"),
        ) as connect:
            Warehouse(WarehouseConfig())
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
