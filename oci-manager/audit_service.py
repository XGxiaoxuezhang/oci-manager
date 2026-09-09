from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import oci

from oci_helpers import build_config, client_kwargs
from storage import fmt_dt
from compartment_scope import compartment_of

# OCI Audit 单次查询的时间跨度上限是 14 天，这里保守取 7 天，
# 每次最多翻 6 页（100 条/页），避免一个请求拖垮页面。
MAX_QUERY_HOURS = 24 * 7
MAX_PAGES = 6
PAGE_SIZE = 100

RANGE_OPTIONS = [
    ("6", "最近 6 小时"),
    ("24", "最近 24 小时"),
    ("72", "最近 3 天"),
    ("168", "最近 7 天"),
]


def get_audit_client(tenant_cfg: dict[str, Any]) -> oci.audit.AuditClient:
    return oci.audit.AuditClient(build_config(tenant_cfg), **client_kwargs())


def _rfc3339(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_dict(value: Any) -> dict[str, Any]:
    """把 OCI SDK 的返回统一成字典。

    Audit 的 data / identity / request / response 可能是 dict，也可能是 SDK 的模型对象
    （oci.audit.models.Data 之类），两种都要能读。
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    instance_vars = getattr(value, "__dict__", None)
    if isinstance(instance_vars, dict):
        # oci 模型把真实字段存成 `_event_name` 这种带下划线前缀的实例属性。
        converted: dict[str, Any] = {}
        for key, item in instance_vars.items():
            if key in {"swagger_types", "attribute_map"} or not key.startswith("_"):
                continue
            converted[key.lstrip("_")] = item
        return converted
    return {}


def _pick(source: dict[str, Any], *names: str) -> Any:
    """按候选名依次取值，兼容 SDK 返回 camelCase 与 snake_case 两种形态。"""
    for name in names:
        value = source.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _extract_event(event: Any) -> dict[str, Any]:
    """把一条 AuditEvent 摊平成展示用字典。

    oci-python-sdk 的 AuditEvent 把详情放在 `data` 里，字段是 snake_case
    （event_name / identity.ip_address / response.status …），
    顶层则只有 event_type / event_time / source 三个元信息。
    """
    data = _as_dict(getattr(event, "data", None))
    identity = _as_dict(data.get("identity"))
    response = _as_dict(data.get("response"))
    request = _as_dict(data.get("request"))

    event_type = getattr(event, "event_type", "") or "-"
    event_time = getattr(event, "event_time", None)
    action = _pick(data, "event_name", "eventName") or event_type
    resource = _pick(data, "resource_name", "resourceName", "resource_id", "resourceId")
    if not resource:
        resource = _pick(request, "path", "resourceName") or "-"

    return {
        "raw_time": event_time.isoformat() if hasattr(event_time, "isoformat") else str(event_time or ""),
        "time": fmt_dt(event_time),
        "action": str(action),
        "source": getattr(event, "source", "") or "-",
        "principal": _pick(identity, "principal_name", "principalName", "caller_name", "principal_id") or "-",
        "credential": _pick(identity, "credentials", "credential_name", "credentialId") or "-",
        "auth_type": _pick(identity, "auth_type", "authType") or "-",
        "ip": _pick(identity, "ip_address", "ipAddress") or "-",
        "user_agent": _pick(identity, "user_agent", "userAgent") or "-",
        "resource": str(resource),
        "compartment": _pick(data, "compartment_name", "compartmentName", "compartment_id") or "-",
        "status": str(_pick(response, "status", "responseStatus") or "-"),
        "message": str(_pick(data, "message") or "")[:300],
    }


def list_cloud_events(
    tenant_cfg: dict[str, Any],
    hours: int = 24,
    keyword: str = "",
    compartment_id: str | None = None,
) -> list[dict[str, Any]]:
    """拉取云端审计事件，按时间倒序。"""
    hours = max(1, min(MAX_QUERY_HOURS, hours))
    client = get_audit_client(tenant_cfg)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    target_compartment = compartment_id or compartment_of(tenant_cfg)

    rows: list[dict[str, Any]] = []
    page: str | None = None
    for _ in range(MAX_PAGES):
        kwargs: dict[str, Any] = {}
        if page:
            kwargs["page"] = page
        # 注意：Audit 的 list_events 不接受 limit 参数，只有 page。
        response = client.list_events(
            compartment_id=target_compartment,
            start_time=_rfc3339(start),
            end_time=_rfc3339(now),
            **kwargs,
        )
        rows.extend(_extract_event(event) for event in (getattr(response, "data", None) or []))
        page = (getattr(response, "headers", {}) or {}).get("opc-next-page")
        if not page:
            break

    keyword = (keyword or "").strip().lower()
    if keyword:
        rows = [
            row
            for row in rows
            if keyword
            in " ".join(
                str(row.get(field, "")) for field in ("action", "principal", "credential", "ip", "resource", "source", "message", "compartment")
            ).lower()
        ]

    rows.sort(key=lambda item: item["raw_time"], reverse=True)
    for row in rows:
        row.pop("raw_time", None)
    return rows


def audit_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    principals = Counter(row["principal"] for row in rows if row["principal"] not in {"-", ""})
    ips = Counter(row["ip"] for row in rows if row["ip"] not in {"-", ""})
    actions = Counter(row["action"] for row in rows if row["action"] not in {"-", ""})
    return {
        "total": len(rows),
        "top_principals": principals.most_common(5),
        "top_ips": ips.most_common(5),
        "top_actions": actions.most_common(5),
    }


def cloud_audit_context(
    tenant_cfg: dict[str, Any],
    hours: int = 24,
    keyword: str = "",
    compartment_id: str | None = None,
) -> dict[str, Any]:
    rows = list_cloud_events(tenant_cfg, hours, keyword, compartment_id)
    return {
        "audit_events": rows[:500],
        "audit_summary": audit_summary(rows),
        "audit_hours": hours,
        "audit_keyword": keyword,
        "audit_range_options": RANGE_OPTIONS,
        "audit_truncated": len(rows) > 500,
    }
