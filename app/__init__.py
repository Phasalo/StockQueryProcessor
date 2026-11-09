from flask import Flask

from config import warehouse_config
from database.reports import WarehouseReports
from .formatting import format_value, sort_value

app = Flask(__name__)
reports = WarehouseReports(warehouse_config)
app.jinja_env.filters["report_value"] = format_value
app.jinja_env.filters["sort_value"] = sort_value

from .views import ui

app.register_blueprint(ui)
