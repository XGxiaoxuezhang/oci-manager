from __future__ import annotations

from typing import Any

from flask import render_template, session

from app_settings import offline_mode_enabled
from settings import LAUNCH_PRESETS
from storage import load_tenants

PAGE_TEMPLATES = {
    "account": "account.html",
    "login": "login.html",
    "home": "home.html",
    "tenants": "tenants.html",
    "module_overview": "module_overview.html",
    "object_storage_overview": "module_overview.html",
    "databases_overview": "module_overview.html",
    "email_overview": "module_overview.html",
    "instance_overview": "instance_overview.html",
    "add_tenant": "add_tenant.html",
    "users": "users.html",
    "instances": "instances.html",
    "instance_edit": "instance_edit.html",
    "change_ip": "change_ip.html",
    "rescue": "rescue.html",
    "novnc": "novnc.html",
    "object_storage": "object_storage.html",
    "object_preview": "object_preview.html",
    "databases": "databases.html",
    "email": "email.html",
    "audit": "audit.html",
    "system": "system.html",
    "settings": "settings.html",
    "backup": "backup.html",
    "cost": "cost.html",
    "checks": "checks.html",
    "idle": "idle.html",
    "security_lists": "security_lists.html",
    "security_rules": "security_rules.html",
    "public_ips": "public_ips.html",
    "cloud_audit": "cloud_audit.html",
    "network": "network.html",
    "nsg": "nsg.html",
    "nsg_rules": "nsg_rules.html",
    "volumes": "volumes.html",
    "console_history": "console_history.html",
}


def render_page(page: str, **context: Any):
    tenants = load_tenants()
    return render_template(
        PAGE_TEMPLATES.get(page, f"{page}.html"),
        page=page,
        current_user=session.get("username"),
        offline_mode=offline_mode_enabled(),
        launch_presets=LAUNCH_PRESETS,
        tenant_options=sorted(tenants.keys()),
        **context,
    )
