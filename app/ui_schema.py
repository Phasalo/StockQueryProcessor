def column(title, key, formatter="text"):
    return {"title": title, "data": key, "formatter": formatter}


SUMMARY_COLUMNS = [
    column("Марка", "brand"),
    column("Формат, мм", "format", "int"),
    column("Толщина, мкм", "thickness", "int"),
    column("Σ Планируемый расход", "planned_consumption_kg", "kg"),
    column("Физический остаток", "on_hand_kg", "kg"),
    column("Свободный остаток", "free_balance_kg", "kg"),
    column("Σ Планируемый приход", "planned_receipt_kg", "kg"),
    column("Прогнозный остаток", "projected_kg", "kg"),
]
CONSUMPTION_COLUMNS = [
    column("Заказчик", "customer_name"),
    column("Карта производителя", "prod_card_number"),
    column("Количество, кг", "delta_kg", "kg"),
]
RECEIPT_COLUMNS = [
    column("Поставщик", "supplier_name"),
    column("Планируемая дата", "planned_date", "date"),
    column("Количество, кг", "delta_kg", "kg"),
]
TRANSACTION_COLUMNS = [
    column("Дата", "created_at", "datetime"),
    column("Тип операции", "transaction_label"),
    column("Тип плёнки", "film_label"),
    column("Марка", "film_brand"),
    column("Формат, мм", "film_format", "int"),
    column("Толщина, мкм", "film_thickness", "int"),
    column("Количество, кг", "film_delta_kg", "kg"),
    column("Контрагент", "counterparty"),
    column("Карта производителя", "card"),
    column("Плановая дата", "visible_planned_date", "date"),
    column("Фактическая дата", "visible_actual_date", "date"),
]
