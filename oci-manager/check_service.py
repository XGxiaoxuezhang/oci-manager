from __future__ import annotations

import json
import os
from typing import Any

from oci_helpers import find_tenant_config, get_identity_client
from settings import CHECKS_PATH
from storage import load_tenants, now_iso
from timeout_utils import run_with_timeout

# 巡检跑在调度线程里，而调度器只有一条线程：任一租户卡住，后面所有定时任务
# （定时启停 / 成本快照）都会静默失效。所以每个租户都必须有独立的等待上限。
_CHECK_TIMEOUT_SECONDS = 15


def run_checks() -> dict[str, Any]:
    results = {"time": now_iso(), "tenants": []}
    for tenant_name in load_tenants():
        item: dict[str, Any] = {"tenant_name": tenant_name, "status": "ok", "message": "API 连通正常"}
        try:
            tenant_cfg = find_tenant_config(tenant_name)
            if tenant_cfg is None:
                raise ValueError("租户配置不存在")
            run_with_timeout(
                _CHECK_TIMEOUT_SECONDS,
                lambda cfg=tenant_cfg: get_identity_client(cfg).get_tenancy(cfg["tenant_id"]),
            )
        except Exception as exc:
            item["status"] = "error"
            item["message"] = str(exc)
        results["tenants"].append(item)
    save_checks(results)
    return results


def save_checks(results: dict[str, Any]) -> None:
    # 先写临时文件再原子替换：写一半崩溃会留下半截 JSON，
    # load_checks 解析失败丢掉上次基线，下一轮会把全部 error 租户当"首见"重复告警
    tmp_path = CHECKS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, CHECKS_PATH)


def load_checks() -> dict[str, Any] | None:
    if not CHECKS_PATH.exists():
        return None
    try:
        return json.loads(CHECKS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
