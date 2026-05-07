from __future__ import annotations

from flask import Blueprint

from audit import list_audit_events
from rendering import render_page

audit_bp = Blueprint("audit", __name__)


@audit_bp.route("/audit")
def audit_home():
    return render_page("audit", events=list_audit_events())
