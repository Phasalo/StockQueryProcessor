from database.repositories.catalog import (
    CustomerRepository,
    FilmTypeRepository,
    MaterialRepository,
    SupplierRepository,
)
from database.repositories.movements import MovementRepository
from database.repositories.plans import PlanRepository
from database.repositories.reports import ReportRepository
from database.repositories.transactions import TransactionRepository

__all__ = [
    "CustomerRepository",
    "FilmTypeRepository",
    "MaterialRepository",
    "SupplierRepository",
    "TransactionRepository",
    "PlanRepository",
    "MovementRepository",
    "ReportRepository",
]
