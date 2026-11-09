from datetime import date, datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE


def workbook(sheets, parameters=None):
    generated_at = datetime.now()
    book = Workbook()
    book.remove(book.active)
    
    for name, columns, rows in sheets:
        sheet = book.create_sheet(name)
        if parameters:
            sheet.append(parameters)
        sheet.append([f"Отчёт сформирован: {generated_at:%d.%m.%Y %H:%M}"])
        sheet.append([col['title'] for col in columns])
        header_row = sheet.max_row
        sheet.freeze_panes = f'A{header_row + 1}'
        for source in rows:
            values = []
            for col in columns:
                value = source.get(col['data'])
                fmt = col.get('formatter', 'text')
                if value is None or value == '':
                    value = '—'
                elif fmt in ('date', 'datetime'):
                    if not isinstance(value, (date, datetime)):
                        value = datetime.fromisoformat(str(value))
                    value = value.strftime('%d.%m.%Y %H:%M' if fmt == 'datetime' else '%d.%m.%Y')
                values.append(value)
            sheet.append(values)
            for cell, col in zip(sheet[sheet.max_row], columns):
                if isinstance(cell.value, str):
                    cell.value = ILLEGAL_CHARACTERS_RE.sub('', cell.value)
                    cell.data_type = 's'
                elif col.get('formatter') == 'kg':
                    cell.number_format = '0.00'
        sheet.auto_filter.ref = f"A{header_row}:{sheet.cell(sheet.max_row, len(columns)).coordinate}"
        for col in sheet.columns:
            header = col[header_row - 1]
            sheet.column_dimensions[header.column_letter].width = max(22, len(str(header.value)) + 3)
    output = BytesIO()
    book.save(output)
    output.seek(0)
    return output
