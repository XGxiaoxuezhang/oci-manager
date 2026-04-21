from __future__ import annotations

import re

from flask import Blueprint, flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from rendering import render_page
from storage import ensure_auth_settings, load_auth_settings, now_iso, save_auth_settings

auth_bp = Blueprint("auth", __name__)

_SAFE_NEXT_RE = re.compile(r"^/[^/\\]")


def _safe_next(next_url: str | None) -> str:
    """Only redirect to relative paths on the same host."""
    if next_url and _SAFE_NEXT_RE.match(next_url):
        return next_url
    return url_for("tenant.index")


@auth_bp.before_app_request
def require_login():
    ensure_auth_settings()
    if request.endpoint in {"auth.login", "static"}:
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
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == settings.get("username") and check_password_hash(
            settings.get("password_hash", ""), password
        ):
            session["authenticated"] = True
            session["username"] = username
            # Prevent open redirect
            return redirect(_safe_next(request.args.get("next")))
        flash("用户名或密码错误。", "error")
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
    return redirect(url_for("tenant.index"))
