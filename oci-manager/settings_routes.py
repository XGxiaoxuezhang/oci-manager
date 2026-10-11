from __future__ import annotations

import os

from flask import Blueprint, flash, redirect, request, url_for

from app_settings import app_setting_enabled, load_app_settings, save_app_settings
from notify import notification_targets, notify, valid_notification_url
from rendering import render_page
from settings import DATA_DIR, SQLITE_PATH

settings_bp = Blueprint("settings_page", __name__)


@settings_bp.route("/settings")
def settings_home():
    env_items = [
        ("OCI_MANAGER_DATA_DIR", str(DATA_DIR)),
        ("SQLITE_PATH", str(SQLITE_PATH)),
        ("OCI_MANAGER_HOST", os.environ.get("OCI_MANAGER_HOST", "0.0.0.0")),
        ("OCI_MANAGER_PORT", os.environ.get("OCI_MANAGER_PORT", "5080")),
        ("OCI_MANAGER_DEBUG", os.environ.get("OCI_MANAGER_DEBUG", "0")),
        ("OCI_MANAGER_SESSION_HOURS", os.environ.get("OCI_MANAGER_SESSION_HOURS", "12")),
        ("OCI_MANAGER_SESSION_COOKIE_SECURE", os.environ.get("OCI_MANAGER_SESSION_COOKIE_SECURE", "0")),
        ("OCI_MANAGER_LOGIN_MAX_FAILURES", os.environ.get("OCI_MANAGER_LOGIN_MAX_FAILURES", "8")),
        ("OCI_MANAGER_LOGIN_WINDOW_SECONDS", os.environ.get("OCI_MANAGER_LOGIN_WINDOW_SECONDS", "600")),
        ("OCI_MANAGER_LOGIN_LOCK_SECONDS", os.environ.get("OCI_MANAGER_LOGIN_LOCK_SECONDS", "900")),
        ("OCI_MANAGER_CLEAR_PROXY", os.environ.get("OCI_MANAGER_CLEAR_PROXY", "0")),
        ("OCI_MANAGER_WEBHOOK_URL", "set" if os.environ.get("OCI_MANAGER_WEBHOOK_URL") else ""),
        ("OCI_MANAGER_NTFY_SERVER", os.environ.get("OCI_MANAGER_NTFY_SERVER", "")),
        ("OCI_MANAGER_NTFY_TOPIC", "set" if os.environ.get("OCI_MANAGER_NTFY_TOPIC") else ""),
    ]
    app_settings = load_app_settings()
    return render_page(
        "settings",
        env_items=env_items,
        app_settings=app_settings,
        notification_targets=notification_targets(),
    )


@settings_bp.route("/settings/notifications", methods=["POST"])
def notification_save():
    webhook_url = request.form.get("webhook_url", "").strip()
    ntfy_server = request.form.get("ntfy_server", "").strip()
    # 只做 scheme 级校验（不解析 DNS），离线环境也能正常保存；发送时 notify 再做完整校验
    if webhook_url and not valid_notification_url(webhook_url):
        flash("Webhook 地址无效：必须是 http(s):// 开头的完整 URL。", "error")
        return redirect(url_for("settings_page.settings_home"))
    if ntfy_server and not valid_notification_url(ntfy_server):
        flash("ntfy 服务器地址无效：必须是 http(s):// 开头的完整 URL。", "error")
        return redirect(url_for("settings_page.settings_home"))
    save_app_settings(
        {
            "offline_mode": "1" if request.form.get("offline_mode") else "0",
            "webhook_url": webhook_url,
            "ntfy_server": ntfy_server,
            "ntfy_topic": request.form.get("ntfy_topic", "").strip(),
        }
    )
    flash("通知设置已保存。", "success")
    return redirect(url_for("settings_page.settings_home"))


@settings_bp.route("/settings/ops", methods=["POST"])
def ops_save():
    keep_raw = request.form.get("auto_backup_keep", "7").strip()
    try:
        keep = max(1, min(30, int(keep_raw)))
    except ValueError:
        flash("备份保留份数必须是数字。", "error")
        return redirect(url_for("settings_page.settings_home"))
    settings = load_app_settings()
    settings["auto_backup_enabled"] = "1" if request.form.get("auto_backup_enabled") else "0"
    settings["auto_backup_keep"] = str(keep)
    save_app_settings(settings)
    flash("运维设置已保存。", "success")
    return redirect(url_for("settings_page.settings_home"))


@settings_bp.route("/settings/notification-test", methods=["POST"])
def notification_test():
    if app_setting_enabled(load_app_settings(), "offline_mode", True):
        flash("离线模式下不会发送外部通知。", "error")
        return redirect(url_for("settings_page.settings_home"))
    targets = notification_targets()
    if not targets["webhook"] and not targets["ntfy"]:
        flash("未配置通知目标。", "error")
        return redirect(url_for("settings_page.settings_home"))
    notify("OCI 管理器通知测试", "这是一条测试通知。", {"source": "settings"})
    flash("测试通知已发送。", "success")
    return redirect(url_for("settings_page.settings_home"))
