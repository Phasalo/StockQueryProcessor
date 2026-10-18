from enum import Enum
from dataclasses import dataclass


class FilmType(Enum):
    BOPP = 'Прозрачный BOPP'
    BOPP_PEARL = 'BOPP жемчужный (перламутровый)'
    BOPP_METAL = 'BOPP металлизированный'
    CPP = 'Каст-полипропилен'
    LDPE_HALF_SLEEVE = 'ПВД полурукав'
    LDPE_FLAT_SHEET = 'ПВД полотно'

    @classmethod
    def get_sql_name(cls) -> str:
        return "enum_film_type"

    @classmethod
    def get_values(cls) -> list[str]:
        return [member.name for member in cls]


@dataclass(frozen=True)
class _TransactionTypeInfo:
    ru_name: str
    description: str
    icon: str
    can_be_positive: bool
    can_be_negative: bool


class TransactionType(Enum):
    CONSUMPTION = _TransactionTypeInfo(
        ru_name="Планируемый расход",
        description="Уменьшение количества (-)",
        icon="📉",
        can_be_positive=False,
        can_be_negative=True
    )

    ADJUSTMENT = _TransactionTypeInfo(
        ru_name="Корректировка",
        description = "Любое изменение количества (+/-)",
        icon="⚖️",
        can_be_positive=True,
        can_be_negative=True
    )

    PLANNED_RECEIPT = _TransactionTypeInfo(
        ru_name="Планируемый приход",
        description="(+) Увеличение количества (+)",
        icon="📅",
        can_be_positive=True,
        can_be_negative=False
    )

    RECEIPT = _TransactionTypeInfo(
        ru_name="Фактический приход",
        description="Увеличение количества (+)",
        icon="✅",
        can_be_positive=True,
        can_be_negative=False
    )

    @classmethod
    def get_sql_name(cls) -> str:
        return "enum_transaction_type"

    @classmethod
    def get_values(cls) -> list[str]:
        return [member.name for member in cls]
