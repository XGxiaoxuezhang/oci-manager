from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import totp as totp_module
from db import login_is_locked, record_login_attempt
from rendering import render_page
from settings import login_lock_seconds, login_max_failures, trusted_proxy_enabled
from storage import auth_sessions_epoch, ensure_auth_settings, load_auth_settings, now_iso, save_auth_settings
from notify import notify

auth_bp = Blueprint("auth", __name__)


def safe_next_url(value: str | None) -> str:
    if not value:
        return url_for("tenant.index")
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/"):
        return url_for("tenant.index")
    return value


def _remote_addr() -> str:
    if trusted_proxy_enabled():
        remote_addr = request.headers.get("X-Forwarded-For", request.remote_addr or "-").split(",")[0].strip()
    else:
        remote_addr = request.remote_addr or "-"
    return remote_addr


def _bump_epoch(settings: dict, session) -> None:
    """改密码 / 关闭 2FA 后递增会话纪元：其他设备的旧 cookie 全部失效。"""
    settings["sessions_epoch"] = int(settings.get("sessions_epoch", 0) or 0) + 1
    session["epoch"] = settings["sessions_epoch"]


@auth_bp.before_app_request
def require_login():
    # ensure_auth_settings() 已移到 app 启动时执行一次，这里不再每请求读一遍 YAML
    if request.endpoint in {"auth.login", "auth.totp_login", "healthz", "chrome_devtools_probe", "static"}:
        return None
    if not session.get("authenticated"):
        return redirect(url_for("auth.login", next=request.path))
    # 密码修改 / 2FA 变更后纪元递增：旧会话（其他设备）自动失效
    if session.get("epoch", 0) != auth_sessions_epoch():
        session.clear()
        flash("登录凭据已变更，请重新登录。", "error")
        return redirect(url_for("auth.login"))
    # 首次部署还没改掉初始密码时，只允许改密和登出
    if session.get("must_change_password") and request.endpoint not in {
        "auth.account",
        "auth.change_password",
        "auth.logout",
    }:
        if request.headers.get("X-Requested-With") == "fetch":
            # fetch 请求拿 302 HTML 会被前端报成"网络错误"，用户无从得知原因
            return {"ok": False, "message": "首次部署请先修改初始登录密码。"}, 401
        flash("首次部署请先修改初始登录密码。", "error")
        return redirect(url_for("auth.account"))
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
            if settings.get("totp_enabled") and settings.get("totp_secret"):
                # 密码正确：进入两步验证待确认态（未认证会话）
                session.clear()
                session["pre_2fa_user"] = username
                session["pre_2fa_at"] = now_iso()
                return redirect(url_for("auth.totp_login"))
            return _complete_login(settings, username, remote_addr)
        record_login_attempt(remote_addr, username, False)
        flash(f"用户名或密码错误。连续失败 {login_max_failures()} 次会临时锁定。", "error")
    return render_page("login", auth_username=settings.get("username", "admin"))


@auth_bp.route("/login/totp", methods=["GET", "POST"])
def totp_login():
    """密码通过后的第二步：输入验证器 6 位动态码。"""
    username = session.get("pre_2fa_user")
    if not username:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        code = request.form.get("code", "")
        settings = load_auth_settings()
        if totp_module.verify(settings.get("totp_secret", ""), code):
            record_login_attempt(_remote_addr(), username, True)
            session.pop("pre_2fa_user", None)
            session.pop("pre_2fa_at", None)
            return _complete_login(settings, username, _remote_addr())
        record_login_attempt(_remote_addr(), username, False)
        flash("动态码不正确，请重试。", "error")
    return render_page("login_totp", auth_username=username)


def _complete_login(settings: dict, username: str, remote_addr: str):
    """会话真正建立：统一处理 2FA 后与直接密码登录两条路径。"""
    record_login_attempt(remote_addr, username, True)
    session.permanent = True
    session["authenticated"] = True
    session["username"] = username
    session["epoch"] = auth_sessions_epoch()
    # 新来源登录推送通知（同一 IP 重复登录不打扰）
    stored_ip = settings.get("last_login_ip", "")
    if stored_ip != remote_addr:
        notify(
            "检测到新的登录",
            f"账号 {username} 于 {now_iso()} 从 {remote_addr} 登录。",
            {"ip": remote_addr, "username": username},
        )
        settings["last_login_ip"] = remote_addr
        save_auth_settings(settings)
    if settings.get("must_change_password"):
        session["must_change_password"] = True
        flash("首次部署请先修改初始登录密码。", "error")
        return redirect(url_for("auth.account"))
    session.pop("must_change_password", None)
    return redirect(safe_next_url(request.args.get("next")))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/account")
def account():
    return render_page("account")


@auth_bp.route("/account/totp/setup", methods=["POST"])
def totp_setup():
    """生成待确认的 TOTP 密钥（存 session，验证通过才落盘）。"""
    settings = load_auth_settings()
    if not check_password_hash(settings.get("password_hash", ""), request.form.get("password", "")):
        flash("当前密码不正确。", "error")
        return redirect(url_for("auth.account"))
    secret = totp_module.generate_secret()
    session["pending_totp_secret"] = secret
    account = settings.get("username", "admin")
    return redirect(url_for("auth.account", totp_pending=1, secret=secret, uri=totp_module.otpauth_uri(secret, account)))


@auth_bp.route("/account/totp/enable", methods=["POST"])
def totp_enable():
    settings = load_auth_settings()
    if not check_password_hash(settings.get("password_hash", ""), request.form.get("password", "")):
        flash("当前密码不正确。", "error")
        return redirect(url_for("auth.account"))
    secret = session.get("pending_totp_secret")
    if not secret:
        flash("请先生成密钥。", "error")
        return redirect(url_for("auth.account"))
    if not totp_module.verify(secret, request.form.get("code", "")):
        flash("动态码不正确，请确认验证器时间后重试。", "error")
        return redirect(url_for("auth.account", totp_pending=1, secret=secret, uri=totp_module.otpauth_uri(secret, settings.get("username", "admin"))))
    settings["totp_secret"] = secret
    settings["totp_enabled"] = True
    settings["updated"] = now_iso()
    _bump_epoch(settings, session)  # 开启 2FA 后其他会话需重新走 2FA
    save_auth_settings(settings)
    session.pop("pending_totp_secret", None)
    flash("两步验证已开启，下次登录需要输入动态码。", "success")
    return redirect(url_for("auth.account"))


@auth_bp.route("/account/totp/disable", methods=["POST"])
def totp_disable():
    settings = load_auth_settings()
    if not check_password_hash(settings.get("password_hash", ""), request.form.get("password", "")):
        flash("当前密码不正确。", "error")
        return redirect(url_for("auth.account"))
    if not settings.get("totp_enabled") or not totp_module.verify(settings.get("totp_secret", ""), request.form.get("code", "")):
        flash("动态码不正确，无法关闭两步验证。", "error")
        return redirect(url_for("auth.account"))
    settings["totp_enabled"] = False
    settings["updated"] = now_iso()
    _bump_epoch(settings, session)
    save_auth_settings(settings)
    flash("两步验证已关闭。", "success")
    return redirect(url_for("auth.account"))


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
    settings["must_change_password"] = False
    settings["updated"] = now_iso()
    _bump_epoch(settings, session)  # 其他设备的旧会话全部失效
    save_auth_settings(settings)
    session.pop("must_change_password", None)
    flash("登录密码已更新，其他设备需要重新登录。", "success")
    return redirect(url_for("auth.account"))
