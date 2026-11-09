from datetime import date, datetime


def format_value(value, kind="text", with_unit=False, signed=False):
    if value is None or value == "":
        return "—"
    if kind == "kg":
        text = format(value, "+,.2f" if signed and value != 0 else ",.2f")
        text = text.replace(",", "\u202f").replace(".", ",").replace("-", "−")
        return text + (" кг" if with_unit else "")
    if kind in ("date", "datetime"):
        if not isinstance(value, (date, datetime)):
            value = datetime.fromisoformat(str(value))
        return value.strftime("%d.%m.%Y %H:%M" if kind == "datetime" else "%d.%m.%Y")
    return str(value)


def sort_value(value):
    if value is None:
        return ""
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value)
