from __future__ import annotations

from flask import Blueprint, request

from cost_service import cost_rules, free_tier_context
from oci_helpers import build_dashboard_cards, find_tenant_config
from rendering import render_page
from storage import load_tenants
from timeout_utils import run_with_timeout

cost_bp = Blueprint("cost", __name__)


@cost_bp.route("/cost")
def cost_home():
    tenants = load_tenants()
    selected_tenant = request.args.get("tenant", "").strip()
    if not selected_tenant and tenants:
        selected_tenant = sorted(tenants.keys())[0]
    context = {"free_items": [], "risks": [], "load_error": ""}
    if selected_tenant:
        tenant_cfg = find_tenant_config(selected_tenant)
        if tenant_cfg is None:
            context["load_error"] = "租户不存在。"
        else:
            try:
                context.update(run_with_timeout(35, free_tier_context, tenant_cfg))
            except Exception as exc:
                context["load_error"] = str(exc)
    return render_page("cost", rules=cost_rules(), tenants=build_dashboard_cards(tenants), selected_tenant=selected_tenant, **context)
