from __future__ import annotations

from flask import Blueprint, request, session

from cost_service import budget_alerts, cost_rules, free_tier_context
from cost_snapshot_service import trend_context
from compartment_service import apply_session_compartment
from oci_helpers import build_dashboard_cards, find_tenant_config
from rendering import render_page
from storage import load_tenants
from timeout_utils import run_with_timeout

cost_bp = Blueprint("cost", __name__)


@cost_bp.route("/cost")
def cost_home():
    tenants = load_tenants()
    selected_tenant = request.args.get("tenant", "").strip()
    if not selected_tenant:
        # 缺省跟随"最近使用的租户"，其次按名称取第一个
        last = session.get("last_tenant")
        selected_tenant = last if last in tenants else (sorted(tenants.keys())[0] if tenants else "")
    if selected_tenant:
        session["last_tenant"] = selected_tenant
    context = {"free_items": [], "risks": [], "load_error": "", "budget_alerts": []}
    if selected_tenant:
        raw_cfg = find_tenant_config(selected_tenant)
        tenant_cfg = apply_session_compartment(raw_cfg, selected_tenant) if raw_cfg else None
        if tenant_cfg is None:
            context["load_error"] = "租户不存在。"
        else:
            try:
                # 免费额度是 tenancy 级账务概念，与每日快照同口径强制按根 compartment 统计，
                # 否则切到子 compartment 时页面会系统性低估
                free_ctx = run_with_timeout(35, free_tier_context, {**tenant_cfg, "_compartment_id": None})
            except Exception as exc:
                context["load_error"] = str(exc)
            else:
                context.update(free_ctx)
                # 只算不外发：GET 不应有外发副作用，告警由后台任务负责推送
                context["budget_alerts"] = budget_alerts(free_ctx)
        # 近 30 天趋势（本地快照，无云端调用）
        try:
            context["trend"] = trend_context(selected_tenant)
        except Exception:
            context["trend"] = {"has_history": False, "trend_items": [], "dates": []}
    else:
        context["trend"] = {"has_history": False, "trend_items": [], "dates": []}
    return render_page("cost", rules=cost_rules(), tenants=build_dashboard_cards(tenants), selected_tenant=selected_tenant, **context)
