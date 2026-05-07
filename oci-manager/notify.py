from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from app_settings import app_setting_enabled, load_app_settings

def notify(title: str, message: str, details: dict[str, Any] | None = None) -> None:
    settings = load_app_settings()
    if app_setting_enabled(settings, "offline_mode", True):
        return
    webhook_url = (settings.get("webhook_url") or os.environ.get("OCI_MANAGER_WEBHOOK_URL", "")).strip()
    ntfy_topic = (settings.get("ntfy_topic") or os.environ.get("OCI_MANAGER_NTFY_TOPIC", "")).strip()
    ntfy_server = (settings.get("ntfy_server") or os.environ.get("OCI_MANAGER_NTFY_SERVER", "")).rstrip("/")
    if webhook_url:
        _post_json(webhook_url, {"title": title, "message": message, "details": details or {}})
    if ntfy_topic:
        _post_text(f"{ntfy_server}/{ntfy_topic}", title, message)


def notification_targets() -> dict[str, bool]:
    settings = load_app_settings()
    if app_setting_enabled(settings, "offline_mode", True):
        return {"webhook": False, "ntfy": False}
    return {
        "webhook": bool((settings.get("webhook_url") or os.environ.get("OCI_MANAGER_WEBHOOK_URL", "")).strip()),
        "ntfy": bool(
            (settings.get("ntfy_topic") or os.environ.get("OCI_MANAGER_NTFY_TOPIC", "")).strip()
            and (settings.get("ntfy_server") or os.environ.get("OCI_MANAGER_NTFY_SERVER", "")).strip()
        ),
    }


def _post_json(url: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    _open_quietly(request)


def _post_text(url: str, title: str, message: str) -> None:
    request = urllib.request.Request(url, data=message.encode("utf-8"), headers={"Title": title}, method="POST")
    _open_quietly(request)


def _open_quietly(request: urllib.request.Request) -> None:
    try:
        urllib.request.urlopen(request, timeout=8).read()
    except Exception:
        return
