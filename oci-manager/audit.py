from __future__ import annotations

import json
from typing import Any

from db import connect
from settings import AUDIT_LOG_PATH
from storage import now_iso

SENSITIVE_KEYS = {"password", "admin_password", "wallet_password", "ssh_public_key", "key_file"}


def _safe_details(details: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in details.items():
        if key in SENSITIVE_KEYS or key.endswith("_password"):
            safe[key] = "[redacted]"
        else:
            safe[key] = value
    return safe


def record_audit(
    action: str,
    *,
    user: str | None = None,
    tenant_name: str | None = None,
    status: str = "success",
    details: dict[str, Any] | None = None,
) -> None:
    event = {
        "time": now_iso(),
        "user": user or "-",
        "tenant_name": tenant_name or "-",
        "action": action,
        "status": status,
        "details": _safe_details(details or {}),
    }
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (time, user, tenant_name, action, status, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event["time"],
                    event["user"],
                    event["tenant_name"],
                    event["action"],
                    event["status"],
                    json.dumps(event["details"], ensure_ascii=False, separators=(",", ":")),
                ),
            )
    except Exception:
        return


def list_audit_events(limit: int = 200) -> list[dict[str, Any]]:
    events = _list_db_events(limit)
    if len(events) < limit:
        legacy = _list_legacy_events(limit - len(events))
        events.extend(legacy)
        events.sort(key=lambda item: item.get("time", ""), reverse=True)
        events = events[:limit]
    return events


def _list_db_events(limit: int) -> list[dict[str, Any]]:
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT time, user, tenant_name, action, status, details_json FROM audit_events ORDER BY time DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except json.JSONDecodeError:
            details = {}
        events.append(
            {
                "time": row["time"],
                "user": row["user"],
                "tenant_name": row["tenant_name"],
                "action": row["action"],
                "status": row["status"],
                "details": details,
            }
        )
    return events


def _list_legacy_events(limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not AUDIT_LOG_PATH.exists():
        return []
    lines = AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    events.reverse()
    return events
