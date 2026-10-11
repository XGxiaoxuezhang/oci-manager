from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta
from logging.handlers import RotatingFileHandler

from flask import Flask, abort, flash, redirect, request, session, url_for

from audit import record_audit
from audit_routes import audit_bp
from backup_routes import backup_bp
from cost_routes import cost_bp
from check_routes import check_bp
from auth_routes import auth_bp
from database_routes import database_bp
from email_routes import email_bp
from object_storage_routes import object_storage_bp
from settings import (
    DATA_DIR,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_MB,
    clear_proxy_enabled,
    debug_enabled,
    server_host,
    server_port,
    secret_key,
    session_cookie_secure,
    session_lifetime,
)
from settings_routes import settings_bp
from storage import fmt_dt
from system_routes import system_bp
from storage import ensure_auth_settings
from tenant_routes import tenant_bp


def clear_broken_proxy_env() -> None:
    if not clear_proxy_enabled():
        return
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)


def format_bytes(value: object) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return "-"


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def setup_logging() -> None:
    """文件日志：scheduler 的 INFO、后台任务的失败原因此前在任何环境都丢失
    （无 handler 时 Python 只输出 WARNING+ 到 stderr；Windows 源码直跑挂服务
    后连 stderr 都没有）。备份会排除 logs/ 目录。"""
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "app.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler, logging.StreamHandler()],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def create_app() -> Flask:
    setup_logging()
    clear_broken_proxy_env()
    app = Flask(__name__)
    app.secret_key = secret_key()
    app.permanent_session_lifetime = session_lifetime()
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=session_cookie_secure(),
        # 静态资源（含 noVNC vendor 约 800KB）默认每次导航都 revalidate，给个合理缓存期；
        # Flask 的 url_for('static') 自带基于文件 mtime 的 v= 版本参数，更新代码不会拿旧缓存
        SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=7),
    )
    app.jinja_env.filters["format_bytes"] = format_bytes
    app.jinja_env.filters["fmt_dt"] = fmt_dt
    app.register_blueprint(auth_bp)
    app.register_blueprint(tenant_bp)
    app.register_blueprint(object_storage_bp)
    app.register_blueprint(database_bp)
    app.register_blueprint(email_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(system_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(backup_bp)
    app.register_blueprint(cost_bp)
    app.register_blueprint(check_bp)

    # 这些原先写在模块末尾，import 即执行；现在集中在启动时跑一次
    from db import bootstrap
    from launch_manager import load_launch_tasks
    from oci_helpers import migrate_tenant_key_paths

    bootstrap()
    load_launch_tasks()
    ensure_auth_settings()
    migrate_tenant_key_paths()

    # 后台调度：巡检定时化 / 资源定时启停 / 成本每日快照
    from scheduler_service import start_scheduler

    start_scheduler()

    @app.context_processor
    def inject_security_helpers():
        return {"csrf_token": csrf_token}

    @app.context_processor
    def inject_compartment():
        """把当前 compartment 切换器注入所有模板，侧栏据此渲染下拉框。"""
        from flask import g

        from compartment_scope import compartment_name_of
        from compartment_service import switcher_context

        tenant_cfg = getattr(g, "tenant_cfg", None)
        tenant_name = tenant_cfg.get("_tenant_name") if tenant_cfg else ""
        if not tenant_name:
            return {"compartment_switcher": None, "compartment_name": ""}
        try:
            switcher = switcher_context(tenant_cfg, tenant_name)
        except Exception:
            switcher = None
        return {"compartment_switcher": switcher, "compartment_name": compartment_name_of(tenant_cfg)}

    @app.before_request
    def verify_csrf_token():
        if request.method != "POST":
            return None
        expected = session.get("csrf_token")
        submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not expected or not submitted or not secrets.compare_digest(str(expected), str(submitted)):
            abort(400, description="CSRF token invalid")
        return None

    @app.after_request
    def audit_mutations(response):
        if request.method == "POST" and request.endpoint not in {"auth.login", "auth.logout"}:
            try:
                record_audit(
                    request.endpoint or request.path,
                    user=session.get("username"),
                    tenant_name=request.view_args.get("tenant_name") if request.view_args else None,
                    status="success" if response.status_code < 400 else "failed",
                    details={
                        "path": request.path,
                        "status_code": response.status_code,
                        "form_keys": sorted(key for key in request.form.keys() if key != "csrf_token"),
                    },
                )
            except OSError:
                pass
        # send_file 也会吃到 SEND_FILE_MAX_AGE_DEFAULT（7 天）：Wallet/rclone 配置
        # 这类含凭据的动态下载绝不能进浏览器缓存，非静态路径一律 no-store
        if not request.path.startswith("/static"):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.errorhandler(413)
    def upload_too_large(error):
        flash(f"单次上传上限 {MAX_UPLOAD_MB} MB，更大文件请用 rclone 或 S3 兼容接口。", "error")
        return redirect(request.referrer or url_for("tenant.index"))

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/.well-known/appspecific/com.chrome.devtools.json")
    def chrome_devtools_probe():
        return ("", 204)

    return app


app = create_app()


if __name__ == "__main__":
    clear_broken_proxy_env()
    host = server_host()
    port = server_port()
    print(f"OCI Manager running at http://{host}:{port}")
    if debug_enabled():
        app.run(debug=True, host=host, port=port)
    else:
        # 非调试一律用 waitress（纯 Python 生产级 WSGI，Docker/Windows 双路径适用）；
        # Werkzeug dev server 单线程且不适合长期挂服务
        from waitress import serve

        serve(app, host=host, port=port, threads=8, connection_limit=200, channel_timeout=120)
