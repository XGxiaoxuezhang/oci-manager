"""成本每日快照：每天存一份各租户的免费额度使用量，供趋势视图回看。

快照存 SQLite cost_snapshots 表（date + tenant 唯一）。
items_json 存 free_tier_context 的 free_items 列表，保留原始结构。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from db import connect
from storage import load_tenants, now_iso

logger = logging.getLogger("scheduler")


def snapshot_all_tenants() -> list[dict[str, Any]]:
    """对全部租户各存一份当日快照（INSERT OR REPLACE，重跑安全）。"""
    from cost_service import free_tier_context
    from oci_helpers import find_tenant_config

    results: list[dict[str, Any]] = []
    for tenant_name in load_tenants():
        tenant_cfg = find_tenant_config(tenant_name)
        if tenant_cfg is None:
            continue
        try:
            # 快照永远针对根 compartment（免费额度是 tenancy 级账务概念）
            ctx = free_tier_context({**tenant_cfg, "_compartment_id": None})
            save_snapshot(tenant_name, ctx.get("free_items", []))
            results.append({"tenant_name": tenant_name, "items": len(ctx.get("free_items", []))})
        except Exception as exc:
            logger.error("租户 %s 成本快照失败: %s", tenant_name, exc)
            results.append({"tenant_name": tenant_name, "error": str(exc)})
    return results


def save_snapshot(tenant_name: str, free_items: list[dict[str, Any]]) -> None:
    today = now_iso()[:10]
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO cost_snapshots (date, tenant_name, items_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (today, tenant_name, json.dumps(free_items, ensure_ascii=False), now_iso()),
        )


def snapshot_history(tenant_name: str, days: int = 30) -> list[dict[str, Any]]:
    """返回近 N 天某租户的快照（含今天的实时视图可以另拉）。

    返回元素：{date, items: [...]}，items 为 free_items 结构（label/used/limit/unit）。
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT date, items_json FROM cost_snapshots
            WHERE tenant_name = ? ORDER BY date DESC LIMIT ?
            """,
            (tenant_name, days),
        ).fetchall()
    return [
        {"date": row["date"], "items": json.loads(row["items_json"])}
        for row in reversed(rows)
    ]


def trend_context(tenant_name: str, days: int = 30) -> dict[str, Any]:
    """把快照历史摊成「按指标」的序列，供模板画趋势。"""
    history = snapshot_history(tenant_name, days)
    if not history:
        return {"has_history": False, "trend_items": [], "dates": []}

    dates = [entry["date"][5:] for entry in history]
    by_key: dict[str, dict[str, Any]] = {}
    for entry in history:
        for item in entry.get("items", []):
            key = item.get("key") or item.get("label") or "-"
            bucket = by_key.setdefault(
                key,
                {
                    "label": item.get("label") or key,
                    "unit": item.get("unit") or "",
                    "limit": item.get("limit"),
                    "series": [],
                },
            )
            bucket["series"].append({"date": entry["date"][5:], "used": item.get("used", 0)})

    trend_items = []
    for bucket in by_key.values():
        used_values = [point["used"] for point in bucket["series"]]
        limit = bucket["limit"]
        peak = max(used_values) if used_values else 0
        latest = used_values[-1] if used_values else 0
        bucket["peak"] = peak
        bucket["latest"] = latest
        bucket["percent_peak"] = round(peak / limit * 100, 1) if limit else None
        # 只有波动过的指标才值得看趋势（一直 0 的没意义）
        if peak > 0:
            trend_items.append(bucket)

    trend_items.sort(key=lambda item: -(item["percent_peak"] or 0))
    return {"has_history": True, "trend_items": trend_items, "dates": dates}
