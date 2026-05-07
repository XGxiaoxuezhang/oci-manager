from __future__ import annotations

import json
from typing import Any

from oci_helpers import find_tenant_config, get_identity_client
from settings import CHECKS_PATH
from storage import load_tenants, now_iso


def run_checks() -> dict[str, Any]:
    results = {"time": now_iso(), "tenants": []}
    for tenant_name in load_tenants():
        item: dict[str, Any] = {"tenant_name": tenant_name, "status": "ok", "message": "API 连通正常"}
        try:
            tenant_cfg = find_tenant_config(tenant_name)
            if tenant_cfg is None:
                raise ValueError("租户配置不存在")
            get_identity_client(tenant_cfg).get_tenancy(tenant_cfg["tenant_id"])
        except Exception as exc:
            item["status"] = "error"
            item["message"] = str(exc)
        results["tenants"].append(item)
    save_checks(results)
    return results


def save_checks(results: dict[str, Any]) -> None:
    CHECKS_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def load_checks() -> dict[str, Any] | None:
    if not CHECKS_PATH.exists():
        return None
    try:
        return json.loads(CHECKS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
