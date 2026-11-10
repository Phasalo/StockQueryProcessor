import unittest
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from openpyxl import load_workbook

from app import app, reports
from semantic_entry.modules.warehouse.enums import FilmType, TransactionType


class FixedDatetime(datetime):
    @classmethod
    def now(cls):
        return cls(2026, 11, 4, 2, 40)


class InterfaceTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.summary = {"planned_consumption_kg": Decimal("-12.50"),
                        "free_balance_kg": Decimal("87.50"), "planned_receipt_kg": Decimal("20")}
        defaults = {
            "get_film_type_summary": [{"format": 1200, "thickness": 25, **self.summary}],
            "get_film_summary": self.summary,
            "get_planned_consumption_details": [],
            "get_planned_receipt_details": [],
            "get_available_formats": [1200],
            "get_available_thicknesses": [25],
            "get_available_brands": [],
            "get_all_transactions": [],
        }
        for name, value in defaults.items():
            patcher = patch.object(reports, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def workbook(self, path, timestamp_row=1):
        with patch("app.views.datetime", FixedDatetime), patch("app.xlsx.datetime", FixedDatetime):
            response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn("_04.11.2026_02-40.xlsx", response.headers["Content-Disposition"])
        book = load_workbook(BytesIO(response.data))
        for sheet in book:
            self.assertEqual(sheet.cell(timestamp_row, 1).value, "Отчёт сформирован: 04.11.2026 02:40")
            self.assertEqual(sheet.freeze_panes, f"A{timestamp_row + 2}")
            self.assertTrue(sheet.auto_filter.ref.startswith(f"A{timestamp_row + 1}:"))
        return book

    def test_pages_and_navigation(self):
        for path in ("/", "/film-type/BOPP", "/film-type/BOPP/1200/25", "/transactions"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('href="/transactions"', response.text)
                self.assertNotIn('report-time', response.text)
                self.assertNotIn('Σ План.', response.text)
                self.assertNotIn('Тип плёнки / Формат / Толщина', response.text)
        self.assertEqual(self.client.get("/film-type/UNKNOWN").status_code, 404)
        self.assertEqual(self.client.get("/film-type/UNKNOWN/1200/25").status_code, 404)
        # An absent size combination remains a valid page with empty API data.
        self.assertEqual(self.client.get("/film-type/BOPP/999/999").status_code, 200)

    def test_first_combination(self):
        with patch.object(reports, "get_film_type_summary", return_value=[{"format": 1200, "thickness": 25}]):
            response = self.client.get("/film")
            self.assertEqual(response.location, "/film-type/BOPP/1200/25")
        with patch.object(reports, "get_film_type_summary", return_value=[]):
            self.assertIn("нет записей", self.client.get("/film").text)

    def test_summary_is_rendered_with_data_and_export_link(self):
        html = self.client.get("/film-type/BOPP").text
        self.assertIn('data-href="/film-type/BOPP/1200/25"', html)
        self.assertIn('data-order="-12.50"', html)
        self.assertIn('−12,50', html)
        self.assertIn('href="/exports/film-type/BOPP"', html)
        self.assertIn('value="BOPP" selected', html)
        self.assertEqual(self.client.get("/reports/film-types").status_code, 404)

    def test_missing_size_keeps_filter_values(self):
        zeros = {name: Decimal("0") for name in self.summary}
        with patch.object(reports, "get_film_summary", return_value=zeros):
            html = self.client.get("/film-type/LDPE_FLAT_SHEET/1400/60").text
        self.assertIn('value="1400" selected', html)
        self.assertIn('value="60" selected', html)
        self.assertIn('0,00 кг', html)
        self.assertIn('href="/exports/film-type/LDPE_FLAT_SHEET/1400/60"', html)

    def test_transaction_html_formats_dates_signs_and_escapes_text(self):
        base = dict(created_at=datetime(2026, 10, 18, 22, 8, 11, 264825),
                    film_type=FilmType.BOPP, film_brand='<script>alert(1)</script>',
                    film_format=1200, film_thickness=25, supplier_name="Поставщик",
                    planned_date=date(2026, 10, 23), actual_date=None)
        rows = [dict(base, transaction_type=TransactionType.RECEIPT, film_delta_kg=Decimal("1234.50")),
                dict(base, transaction_type=TransactionType.CONSUMPTION, film_delta_kg=Decimal("-1234.50"))]
        with patch.object(reports, "get_all_transactions", return_value=rows):
            html = self.client.get("/transactions").text
        self.assertIn('18.10.2026 22:08', html)
        self.assertIn('23.10.2026', html)
        self.assertIn('class="text-success">+1\u202f234,50</span>', html)
        self.assertIn('class="text-danger">−1\u202f234,50</span>', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_report_layout(self):
        for path in ("/film-type/BOPP", "/film-type/BOPP/1200/25", "/transactions"):
            with self.subTest(path=path):
                html = self.client.get(path).text
                sections = [html.index('<nav '), html.index('<header '),
                            html.index('aria-label="Поиск и выгрузка"')]
                if path != "/transactions":
                    sections.append(html.index('id="film-type"'))
                sections.append(html.index('<table '))
                self.assertEqual(sections, sorted(sections))
                self.assertEqual(html.count('<h1 '), 1)
                self.assertEqual(html.count('id="report-search"'), 1)
                self.assertEqual(html.count('id="download"'), 1)
        detail = self.client.get("/film-type/BOPP/1200/25").text
        self.assertNotIn('id="page-context"', detail)
        self.assertIn('id="summary-link" href="/film-type/BOPP"', detail)

    def test_summary_export_contains_all_rows_and_numbers(self):
        rows = [{"format": 1200, "thickness": 25, **self.summary} for _ in range(30)]
        with patch.object(reports, "get_film_type_summary", return_value=rows) as query:
            book = self.workbook("/exports/film-type/BOPP", timestamp_row=2)
            query.assert_called_once_with(FilmType.BOPP)
        sheet = book.active
        self.assertEqual(sheet["A1"].value, "Тип плёнки: Прозрачный BOPP")
        self.assertEqual(sheet.max_row, 33)
        self.assertEqual(sheet.max_column, 8)
        self.assertEqual(sheet["D4"].value, -12.5)
        self.assertEqual(sheet["D4"].data_type, "n")
        self.assertEqual(sheet["D4"].number_format, "0.00")

    def test_ambiguous_detail_requires_brand_and_disables_export(self):
        with patch.object(reports, "get_available_brands", return_value=["Brand", "Other"]), \
             patch.object(reports, "get_film_summary") as summary:
            response = self.client.get("/film-type/BOPP/1200/25")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Выберите марку", response.text)
            self.assertIn('aria-disabled="true"', response.text)
            self.assertNotIn('href="/exports/film-type/BOPP/1200/25"', response.text)
            self.assertEqual(self.client.get("/exports/film-type/BOPP/1200/25").status_code, 400)
            self.assertEqual(self.client.get("/film-type/BOPP/1200/25?brand=Missing").status_code, 404)
            summary.assert_not_called()

    def test_selected_brand_is_passed_to_reports_and_export_link(self):
        with patch.object(reports, "get_available_brands", return_value=["A&B", "Other"]), \
             patch.object(reports, "get_film_summary", return_value=self.summary) as summary, \
             patch.object(reports, "get_planned_consumption_details", return_value=[]) as outgoing, \
             patch.object(reports, "get_planned_receipt_details", return_value=[]) as incoming:
            response = self.client.get("/film-type/BOPP/1200/25?brand=A%26B")
            self.assertEqual(response.status_code, 200)
            self.assertIn('value="A&amp;B" selected', response.text)
            self.assertIn('href="/exports/film-type/BOPP/1200/25?brand=A%26B"', response.text)
            for method in (summary, outgoing, incoming):
                method.assert_called_once_with(FilmType.BOPP, 1200, 25, brand="A&B")

    def test_detail_export(self):
        with patch.object(reports, "get_film_summary", return_value=self.summary), \
             patch.object(reports, "get_planned_consumption_details", return_value=[
                 {"customer_name": "=1+1", "prod_card_number": None, "delta_kg": Decimal("-12.5")}
             ]), patch.object(reports, "get_planned_receipt_details", return_value=[
                 {"supplier_name": "Поставщик", "planned_date": date(2026, 11, 4), "delta_kg": Decimal("20")}
             ]):
            book = self.workbook("/exports/film-type/BOPP/1200/25", timestamp_row=2)
        self.assertEqual(book.sheetnames, ["Сводка", "План расхода", "План прихода"])
        for sheet in book:
            self.assertEqual([cell.value for cell in sheet[1]][:3], [
                "Тип плёнки: Прозрачный BOPP", "Формат: 1200 мм", "Толщина: 25 мкм",
            ])
        self.assertEqual(book["Сводка"]["A3"].value, "Σ Планируемый расход")
        self.assertEqual(book["План расхода"]["C4"].value, -12.5)
        self.assertEqual(book["План расхода"]["C4"].data_type, "n")
        self.assertEqual(book["План расхода"]["C4"].number_format, "0.00")
        self.assertEqual(book["План расхода"]["A4"].data_type, "s")
        self.assertEqual(book["План расхода"]["B4"].value, "—")
        self.assertEqual(book["План прихода"]["B4"].value, "04.11.2026")

    def test_transaction_export_type_rules(self):
        rows = [dict(transaction_type=kind, film_type=FilmType.BOPP, film_brand="Марка",
                     created_at=datetime(2026, 11, 4, 12, 30), film_format=1200, film_thickness=25,
                     film_delta_kg=Decimal("-1.25"), customer_name="Заказчик",
                     customer_prod_card_number="001", supplier_name="Поставщик",
                     planned_date=date(2026, 11, 5), actual_date=date(2026, 11, 6)) for kind in TransactionType]
        with patch.object(reports, "get_all_transactions", return_value=rows):
            sheet = self.workbook("/exports/transactions").active
        self.assertEqual(sheet.max_column, 11)
        self.assertEqual(sheet["A3"].value, "04.11.2026 12:30")
        self.assertEqual(sheet["H3"].value, "Заказчик")
        self.assertEqual(sheet["I3"].value, "001")
        self.assertEqual(sheet["J3"].value, "—")
        self.assertEqual(sheet["H4"].value, "—")
        self.assertEqual(sheet["H5"].value, "Поставщик")
        self.assertEqual(sheet["K5"].value, "—")
        self.assertEqual(sheet["K6"].value, "06.11.2026")
        self.assertEqual(sheet["H7"].value, "Заказчик")
        self.assertEqual(sheet["I7"].value, "001")
        self.assertEqual(sheet["J7"].value, "—")
        self.assertEqual(sheet["K7"].value, "06.11.2026")

    def test_empty_exports(self):
        with patch.object(reports, "get_all_transactions", return_value=[]):
            self.assertEqual(self.workbook("/exports/transactions").active.max_row, 2)


if __name__ == "__main__":
    unittest.main()
