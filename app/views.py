from datetime import datetime

from flask import Blueprint, abort, redirect, render_template, send_file, url_for

from database.enums import FilmType, TransactionType
from . import reports
from .ui_schema import SUMMARY_COLUMNS, CONSUMPTION_COLUMNS, RECEIPT_COLUMNS, TRANSACTION_COLUMNS, column
from .xlsx import workbook

ui = Blueprint("ui", __name__)
FILM_TYPES = [(ft.name, ft.value) for ft in FilmType]


def film_type(raw):
    try:
        return FilmType[raw]
    except KeyError:
        abort(404)


def transactions():
    rows = reports.get_all_transactions()
    for row in rows:
        kind = row["transaction_type"]
        consumption = kind in (TransactionType.CONSUMPTION, TransactionType.ISSUE)
        receipt = kind in (TransactionType.RECEIPT, TransactionType.PLANNED_RECEIPT)
        film = row["film_type"]
        row.update(
            transaction_label=kind.value.ru_name,
            film_label=row.get("film_type_name", film.value if isinstance(film, FilmType) else film),
            counterparty=row.get("customer_name") if consumption else row.get("supplier_name") if receipt else None,
            card=row.get("customer_prod_card_number") if consumption else None,
            visible_planned_date=row.get("planned_date") if receipt else None,
            visible_actual_date=row.get("actual_date") if kind in (TransactionType.RECEIPT, TransactionType.ISSUE) else None,
        )
    return rows


def spreadsheet(sheets, name, parameters=None):
    now = datetime.now()
    response = send_file(workbook(sheets, parameters), download_name=f"{name}_{now:%d.%m.%Y_%H-%M}.xlsx", as_attachment=True,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response.headers["Cache-Control"] = "no-store"
    return response


@ui.get("/")
def home():
    return render_template("home.html", page_title="Склад плёнки", page_kind="home")


@ui.get("/film")
def first_film():
    rows = reports.get_film_type_summary(FilmType.BOPP)
    if not rows:
        return render_template("error.html", page_title="Нет записей", message="Для этого типа плёнок нет записей", page_kind="error")
    first = rows[0]
    return redirect(url_for("ui.detail", raw="BOPP", film_format=first["format"], thickness=first["thickness"]))


@ui.get("/film-type/<raw>")
def summary(raw):
    ft = film_type(raw)
    return render_template("summary.html", page_title="Тип плёнки", page_kind="summary",
                           columns=SUMMARY_COLUMNS, rows=reports.get_film_type_summary(ft),
                           film_types=FILM_TYPES, selected_type=ft.name,
                           export_url=url_for("ui.export_summary", raw=raw))


@ui.get("/film-type/<raw>/<int:film_format>/<int:thickness>")
def detail(raw, film_format, thickness):
    ft = film_type(raw)
    args = ft, film_format, thickness
    return render_template("detail.html", page_title="Детализация плёнки", page_kind="detail",
                           film_types=FILM_TYPES, selected_type=ft.name,
                           formats=[(value, value) for value in reports.get_available_formats(ft)],
                           thicknesses=[(value, value) for value in reports.get_available_thicknesses(ft)],
                           selected_format=film_format, selected_thickness=thickness,
                           summary=reports.get_film_summary(*args),
                           consumption_columns=CONSUMPTION_COLUMNS,
                           consumption_rows=reports.get_planned_consumption_details(*args),
                           receipt_columns=RECEIPT_COLUMNS,
                           receipt_rows=reports.get_planned_receipt_details(*args),
                           export_url=url_for("ui.export_detail", raw=raw, film_format=film_format, thickness=thickness))


@ui.get("/transactions")
def journal():
    return render_template("transactions.html", page_title="Все записи", page_kind="transactions",
                           columns=TRANSACTION_COLUMNS, rows=transactions(),
                           export_url=url_for("ui.export_transactions"))


@ui.get("/exports/film-type/<raw>")
def export_summary(raw):
    ft = film_type(raw)
    rows = reports.get_film_type_summary(ft)
    return spreadsheet([("Сводка", SUMMARY_COLUMNS, rows)], raw, parameters=[f"Тип плёнки: {ft.value}"])


@ui.get("/exports/film-type/<raw>/<int:film_format>/<int:thickness>")
def export_detail(raw, film_format, thickness):
    args = film_type(raw), film_format, thickness
    summary_rows = reports.get_film_summary(*args)
    summary_columns = [column("Σ Планируемый расход", "planned_consumption_kg", "kg"),
                       column("Свободный остаток", "free_balance_kg", "kg"),
                       column("Σ Планируемый приход", "planned_receipt_kg", "kg")]
    return spreadsheet([
        ("Сводка", summary_columns, [summary_rows]),
        ("План расхода", CONSUMPTION_COLUMNS, reports.get_planned_consumption_details(*args)),
        ("План прихода", RECEIPT_COLUMNS, reports.get_planned_receipt_details(*args)),
    ], f"{raw}-{film_format}-{thickness}", parameters=[
        f"Тип плёнки: {args[0].value}", f"Формат: {film_format} мм", f"Толщина: {thickness} мкм",
    ])


@ui.get("/exports/transactions")
def export_transactions():
    return spreadsheet([("Все записи", TRANSACTION_COLUMNS, transactions())], "transactions")


@ui.app_errorhandler(404)
def not_found(error):
    return render_template("error.html", page_title="404", message="Страница не найдена", page_kind="error"), 404


@ui.app_errorhandler(500)
def server_error(error):
    return render_template("error.html", page_title="Ошибка", message="Не удалось загрузить данные. Попробуйте позже.", page_kind="error"), 500
