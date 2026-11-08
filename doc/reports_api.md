# Карта эндпоинтов API отчётов (архив)

> JSON API `/reports/...` удалено при упрощении приложения. Flask теперь напрямую вызывает `WarehouseReports` и возвращает HTML через Jinja. Ниже сохранено прежнее описание для истории. Актуальная архитектура и маршруты описаны в [interface.md](interface.md).

Базовый префикс: `/reports`. Все ответы — JSON. Ошибки — `{"error": "<message>"}` с кодом 400 (невалидный ввод) или 500 (сбой БД).

## Справочники

| Метод | Путь | Параметры | Ответ |
|---|---|---|---|
| GET | `/reports/film-types` | — | `[{name, label}]` — список типов плёнки |
| GET | `/reports/transaction-types` | — | `[{name, ru_name, description, can_be_positive, can_be_negative}]` — типы транзакций |
| GET | `/reports/formats/<film_type>` | `film_type` — имя `FilmType` | `[int]` — доступные форматы |
| GET | `/reports/thicknesses/<film_type>` | `film_type` — имя `FilmType` | `[int]` — доступные толщины |


## Сводки

| Метод | Путь | Параметры | Ответ |
|---|---|---|---|
| GET | `/reports/film-type-summary/<film_type>` | `film_type` — имя `FilmType` | `[{format, thickness, planned_consumption_kg, planned_receipt_kg, free_balance_kg}]` |
| GET | `/reports/film-summary/<film_type>/<film_format>/<thickness>` | `film_type` — имя `FilmType`; `film_format`, `thickness` — int | `{planned_consumption_kg, planned_receipt_kg, free_balance_kg}` |

## Детализация

| Метод | Путь | Параметры | Ответ |
|---|---|---|---|
| GET | `/reports/planned-consumption/<film_type>/<film_format>/<thickness>` | `film_type` — имя `FilmType`; `film_format`, `thickness` — int | `[{customer_name, prod_card_number, delta_kg}]` |
| GET | `/reports/planned-receipt/<film_type>/<film_format>/<thickness>` | `film_type` — имя `FilmType`; `film_format`, `thickness` — int | `[{supplier_name, planned_date, delta_kg}]` |

## Транзакции

| Метод | Путь | Параметры | Ответ |
|---|---|---|---|
| GET | `/reports/transactions` | — | `[{id, created_at, transaction_type, film_type, film_brand, film_format, film_thickness, film_delta_kg, customer_name, customer_prod_card_number, supplier_name, planned_date, actual_date}]` |

## Соглашения

- **`film_type`** — имя члена `FilmType`: `BOPP`, `BOPP_PEARL`, `BOPP_METAL`, `CPP`, `LDPE_HALF_SLEEVE`, `LDPE_FLAT_SHEET`.
- **`transaction_type`** (в ответах) — имя `TransactionType`: `CONSUMPTION`, `ADJUSTMENT`, `PLANNED_RECEIPT`, `RECEIPT`.
- **Сериализация**: `Enum` → `.name`; `date/datetime` → ISO‑строка; `Decimal` → `float`.
- **Планируемый расход**: `delta_kg` в `/reports/planned-consumption/...` отрицательный, со знаком исходной транзакции.
