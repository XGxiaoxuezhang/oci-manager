from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from app_settings import app_setting_enabled, load_app_settings


def valid_notification_url(url: str) -> bool:
    """通知地址至少要是合法的 http(s) URL（保存设置时的快速校验，不做 DNS 解析）。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def safe_notification_url(url: str) -> bool:
    """发送前的完整校验：http(s) 且解析出的 IP 不是链路本地/保留/多播地址。

    重点挡 169.254.169.254 这类云元数据端点。私网与环回放行——自建 ntfy
    通常就在局域网或本机（README 明确支持）。
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        infos = socket.getaddrinfo(parsed.hostname, None)
    except (ValueError, OSError, UnicodeError):
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
            return False
    return True


def notify(title: str, message: str, details: dict[str, Any] | None = None) -> None:
    settings = load_app_settings()
    if app_setting_enabled(settings, "offline_mode", True):
        return
    webhook_url = (settings.get("webhook_url") or os.environ.get("OCI_MANAGER_WEBHOOK_URL", "")).strip()
    ntfy_topic = (settings.get("ntfy_topic") or os.environ.get("OCI_MANAGER_NTFY_TOPIC", "")).strip()
    ntfy_server = (settings.get("ntfy_server") or os.environ.get("OCI_MANAGER_NTFY_SERVER", "")).rstrip("/")
    if webhook_url:
        if safe_notification_url(webhook_url):
            _post_json(webhook_url, {"title": title, "message": message, "details": details or {}})
        else:
            print(f"[notify] 已跳过不安全的通知地址: {webhook_url}", flush=True)
    if ntfy_topic:
        target = f"{ntfy_server}/{ntfy_topic}"
        if safe_notification_url(target):
            _post_text(target, title, message)
        else:
            print(f"[notify] 已跳过不安全的通知地址: {ntfy_server}", flush=True)


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
