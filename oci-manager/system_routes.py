from __future__ import annotations

from flask import Blueprint

from rendering import render_page
from system_service import system_context

system_bp = Blueprint("system", __name__)


@system_bp.route("/system")
def system_home():
    return render_page("system", **system_context())
