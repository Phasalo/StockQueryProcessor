from pydantic import Field, StrictInt

from semantic_entry.core import CommandEffect, CommandModel, CommandSpec
from semantic_entry.modules.warehouse.models import StockBalance


class GetStockInput(CommandModel):
    material_id: StrictInt = Field(gt=0)


class GetStockResult(CommandModel):
    stock: StockBalance | None


GET_STOCK_SPEC = CommandSpec(
    name="warehouse.get_stock",
    version=1,
    effect=CommandEffect.READ,
    input_model=GetStockInput,
    result_model=GetStockResult,
    description="Получить физический остаток, резерв, доступный и прогнозный остатки материала в кг.",
    permission="warehouse.stock.read",
)
