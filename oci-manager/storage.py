from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

import yaml
from werkzeug.security import generate_password_hash

from settings import AUTH_PATH, CONFIG_PATH


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def fmt_dt(value: Any) -> str:
    if value is None:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value).replace("T", " ")[:16]


def ensure_auth_settings() -> dict[str, Any]:
    if AUTH_PATH.exists():
        with AUTH_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if data.get("username") and data.get("password_hash"):
            return data

    username = os.environ.get("OCI_MANAGER_USERNAME", "admin")
    password = os.environ.get("OCI_MANAGER_PASSWORD", "admin123456")
    data = {
        "username": username,
        "password_hash": generate_password_hash(password),
        # 首次部署生成的初始口令必须改掉；改密成功后由 change_password 置回 False
        "must_change_password": True,
        "created": now_iso(),
    }
    save_auth_settings(data)
    return data


def load_auth_settings() -> dict[str, Any]:
    return ensure_auth_settings()


def restrict_file_permissions(path) -> None:
    """凭据类文件落盘后收紧权限；Windows 上 chmod 近似 no-op，失败可忽略。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_auth_settings(data: dict[str, Any]) -> None:
    with AUTH_PATH.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    restrict_file_permissions(AUTH_PATH)


def load_tenants() -> dict[str, dict[str, Any]]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def save_tenants(tenants: dict[str, dict[str, Any]]) -> None:
    ordered = dict(sorted(tenants.items(), key=lambda item: item[0].lower()))
    with CONFIG_PATH.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(ordered, fh, allow_unicode=True, sort_keys=False)
    restrict_file_permissions(CONFIG_PATH)


# Windows 保留设备名：作为目录名会导致 key_file.save 行为异常
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def normalize_tenant_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip())
    cleaned = cleaned.strip("-").rstrip(".")
    # "." 和 ".." 在路径拼接里有特殊含义；空串让调用方走"名称不合法"分支
    if cleaned in {"", ".", ".."}:
        return ""
    if cleaned.lower() in _WINDOWS_RESERVED:
        return ""
    return cleaned
