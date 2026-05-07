from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

import oci

from settings import (
    AUTH_PATH,
    CONFIG_PATH,
    DATA_DIR,
    OCI_CONNECT_TIMEOUT,
    OCI_READ_TIMEOUT,
    SQLITE_PATH,
    TENANT_DIR,
    clear_proxy_enabled,
    debug_enabled,
    login_max_failures,
    login_window_seconds,
    server_host,
    server_port,
    session_cookie_secure,
)
from storage import load_tenants


def _status(ok: bool) -> str:
    return "ok" if ok else "warn"


def system_context() -> dict[str, Any]:
    tenants = load_tenants()
    tenant_checks = []
    for name, cfg in tenants.items():
        key_path = Path(cfg.get("key_path") or TENANT_DIR / name / "key.pem")
        fallback_key_path = TENANT_DIR / name / "key.pem"
        effective_key_path = key_path if key_path.exists() else fallback_key_path
        tenant_checks.append(
            {
                "name": name,
                "region": cfg.get("region", "-"),
                "key_path": str(effective_key_path),
                "key_exists": effective_key_path.exists(),
                "has_required_fields": all(cfg.get(key) for key in ("tenant_id", "user_id", "region", "fingerprint")),
            }
        )

    secret_value = os.environ.get("OCI_MANAGER_SECRET_KEY")
    secret_is_placeholder = secret_value in {None, "", "change-this-secret-key", "oci-manager-dev-key"}
    checks = [
        {"label": "Python", "value": sys.version.split()[0], "status": "ok"},
        {"label": "平台", "value": platform.platform(), "status": "ok"},
        {"label": "OCI SDK", "value": getattr(oci, "__version__", "unknown"), "status": "ok"},
        {"label": "数据目录", "value": str(DATA_DIR), "status": _status(DATA_DIR.exists())},
        {"label": "登录配置", "value": str(AUTH_PATH), "status": _status(AUTH_PATH.exists())},
        {"label": "租户配置", "value": str(CONFIG_PATH), "status": _status(CONFIG_PATH.exists())},
        {"label": "租户私钥目录", "value": str(TENANT_DIR), "status": _status(TENANT_DIR.exists())},
        {"label": "SQLite", "value": str(SQLITE_PATH), "status": _status(SQLITE_PATH.exists())},
        {"label": "监听地址", "value": f"{server_host()}:{server_port()}", "status": "ok"},
        {"label": "Debug", "value": "on" if debug_enabled() else "off", "status": "warn" if debug_enabled() else "ok"},
        {"label": "清理代理", "value": "on" if clear_proxy_enabled() else "off", "status": "warn" if clear_proxy_enabled() else "ok"},
        {"label": "OCI 超时", "value": f"{OCI_CONNECT_TIMEOUT}s / {OCI_READ_TIMEOUT}s", "status": "ok"},
        {"label": "登录保护", "value": f"{login_max_failures()} 次 / {login_window_seconds()}s", "status": "ok"},
        {"label": "Cookie Secure", "value": "on" if session_cookie_secure() else "off", "status": "ok" if session_cookie_secure() else "warn"},
        {
            "label": "Secret Key",
            "value": "default/placeholder" if secret_is_placeholder else "env",
            "status": "warn" if secret_is_placeholder else "ok",
        },
    ]
    return {"checks": checks, "tenant_checks": tenant_checks}
