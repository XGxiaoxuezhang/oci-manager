from __future__ import annotations

from typing import Any

from flask import render_template, session

from settings import LAUNCH_PRESETS

PAGE_TEMPLATES = {
    "account": "account.html",
    "login": "login.html",
    "tenants": "tenants.html",
    "add_tenant": "add_tenant.html",
    "users": "users.html",
    "instances": "instances.html",
    "change_ip": "change_ip.html",
    "launcher": "launcher.html",
    "rescue": "rescue.html",
    "object_storage": "object_storage.html",
    "object_preview": "object_preview.html",
    "databases": "databases.html",
    "email": "email.html",
    "security_lists": "security_lists.html",
    "security_rules": "security_rules.html",
}


def render_page(page: str, **context: Any):
    return render_template(
        PAGE_TEMPLATES.get(page, f"{page}.html"),
        page=page,
        current_user=session.get("username"),
        launch_presets=LAUNCH_PRESETS,
        **context,
    )
