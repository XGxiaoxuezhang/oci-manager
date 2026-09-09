from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import login_is_locked, record_login_attempt
from rendering import render_page
from settings import login_lock_seconds, login_max_failures, trusted_proxy_enabled
from storage import ensure_auth_settings, load_auth_settings, now_iso, save_auth_settings

auth_bp = Blueprint("auth", __name__)


def safe_next_url(value: str | None) -> str:
    if not value:
        return url_for("tenant.index")
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/"):
        return url_for("tenant.index")
    return value


@auth_bp.before_app_request
def require_login():
    # ensure_auth_settings() 已移到 app 启动时执行一次，这里不再每请求读一遍 YAML
    if request.endpoint in {"auth.login", "healthz", "chrome_devtools_probe", "static"}:
        return None
    if not session.get("authenticated"):
        return redirect(url_for("auth.login", next=request.path))
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("tenant.index"))
    settings = load_auth_settings()
    if request.method == "POST":
        if trusted_proxy_enabled():
            remote_addr = request.headers.get("X-Forwarded-For", request.remote_addr or "-").split(",")[0].strip()
        else:
            remote_addr = request.remote_addr or "-"
        if login_is_locked(remote_addr):
            minutes = max(1, login_lock_seconds() // 60)
            flash(f"失败次数过多，请约 {minutes} 分钟后再试。", "error")
            return render_page("login", auth_username=settings.get("username", "admin"))
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == settings.get("username") and check_password_hash(settings.get("password_hash", ""), password):
            record_login_attempt(remote_addr, username, True)
            session.permanent = True
            session["authenticated"] = True
            session["username"] = username
            return redirect(safe_next_url(request.args.get("next")))
        record_login_attempt(remote_addr, username, False)
        flash(f"用户名或密码错误。连续失败 {login_max_failures()} 次会临时锁定。", "error")
    return render_page("login", auth_username=settings.get("username", "admin"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/account")
def account():
    return render_page("account")


@auth_bp.route("/account/password", methods=["POST"])
def change_password():
    settings = load_auth_settings()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    if not check_password_hash(settings.get("password_hash", ""), current_password):
        flash("当前密码不正确。", "error")
        return redirect(url_for("auth.account"))
    if len(new_password) < 8:
        flash("新密码至少 8 位。", "error")
        return redirect(url_for("auth.account"))
    if new_password != confirm_password:
        flash("两次输入的新密码不一致。", "error")
        return redirect(url_for("auth.account"))
    settings["password_hash"] = generate_password_hash(new_password)
    settings["updated"] = now_iso()
    save_auth_settings(settings)
    flash("登录密码已更新。", "success")
    return redirect(url_for("auth.account"))
