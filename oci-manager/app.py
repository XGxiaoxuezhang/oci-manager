from __future__ import annotations

import os
import secrets

from flask import Flask, abort, request, session

from audit import record_audit
from audit_routes import audit_bp
from backup_routes import backup_bp
from cost_routes import cost_bp
from check_routes import check_bp
from auth_routes import auth_bp
from database_routes import database_bp
from email_routes import email_bp
from object_storage_routes import object_storage_bp
from settings import clear_proxy_enabled, debug_enabled, server_host, server_port, secret_key, session_cookie_secure, session_lifetime
from settings_routes import settings_bp
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


def create_app() -> Flask:
    clear_broken_proxy_env()
    app = Flask(__name__)
    app.secret_key = secret_key()
    app.permanent_session_lifetime = session_lifetime()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=session_cookie_secure(),
    )
    app.jinja_env.filters["format_bytes"] = format_bytes
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

    @app.context_processor
    def inject_security_helpers():
        return {"csrf_token": csrf_token}

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
        if response.status_code < 400 and request.method == "POST" and request.endpoint not in {"auth.login", "auth.logout"}:
            try:
                record_audit(
                    request.endpoint or request.path,
                    user=session.get("username"),
                    tenant_name=request.view_args.get("tenant_name") if request.view_args else None,
                    details={
                        "path": request.path,
                        "status_code": response.status_code,
                        "form_keys": sorted(key for key in request.form.keys() if key != "csrf_token"),
                    },
                )
            except OSError:
                pass
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

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
    ensure_auth_settings()
    host = server_host()
    port = server_port()
    print(f"OCI Manager running at http://127.0.0.1:{port}")
    app.run(debug=debug_enabled(), host=host, port=port)
