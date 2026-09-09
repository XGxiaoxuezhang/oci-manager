from __future__ import annotations

from flask import Blueprint, flash, redirect, request, url_for

from check_service import load_checks, run_checks
from idle_service import idle_resources_context
from compartment_service import apply_session_compartment
from oci_helpers import find_tenant_config
from rendering import render_page
from storage import load_tenants
from timeout_utils import run_with_timeout

check_bp = Blueprint("check", __name__)


@check_bp.route("/checks")
def checks_home():
    return render_page("checks", results=load_checks())


@check_bp.route("/checks/run", methods=["POST"])
def checks_run():
    results = run_checks()
    flash("巡检完成。", "success")
    return redirect(url_for("check.checks_home"))


@check_bp.route("/checks/idle")
def idle_home():
    tenants = load_tenants()
    selected_tenant = request.args.get("tenant", "").strip()
    if not selected_tenant and tenants:
        selected_tenant = sorted(tenants.keys())[0]
    context = {"selected_tenant": selected_tenant, "tenant_names": sorted(tenants.keys()), "idle": None, "load_error": ""}
    if selected_tenant:
        raw_cfg = find_tenant_config(selected_tenant)
        tenant_cfg = apply_session_compartment(raw_cfg, selected_tenant) if raw_cfg else None
        if tenant_cfg is None:
            context["load_error"] = "租户不存在。"
        else:
            try:
                context["idle"] = run_with_timeout(40, idle_resources_context, tenant_cfg)
            except Exception as exc:
                context["load_error"] = str(exc)
    return render_page("idle", **context)
