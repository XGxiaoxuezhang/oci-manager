from __future__ import annotations

import os

from flask import Flask

from auth_routes import auth_bp
from database_routes import database_bp
from email_routes import email_bp
from object_storage_routes import object_storage_bp
from settings import secret_key
from storage import ensure_auth_settings
from tenant_routes import tenant_bp


def clear_broken_proxy_env() -> None:
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


def create_app() -> Flask:
    clear_broken_proxy_env()
    app = Flask(__name__)
    app.secret_key = secret_key()

    # ── Security headers ──────────────────────────────────
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        # Only set CSP for HTML responses to avoid breaking downloads
        if "text/html" in response.content_type:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "script-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "connect-src 'self';"
            )
        return response

    app.jinja_env.filters["format_bytes"] = format_bytes
    app.register_blueprint(auth_bp)
    app.register_blueprint(tenant_bp)
    app.register_blueprint(object_storage_bp)
    app.register_blueprint(database_bp)
    app.register_blueprint(email_bp)
    return app


app = create_app()


if __name__ == "__main__":
    clear_broken_proxy_env()
    ensure_auth_settings()
    debug = os.environ.get("OCI_MANAGER_DEBUG", "").lower() in ("1", "true", "yes")
    print("OCI Manager running at http://127.0.0.1:5080")
    if debug:
        print("⚠  Debug mode ON – do not expose to the internet")
    app.run(debug=debug, host="0.0.0.0", port=5080)
