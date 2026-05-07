from __future__ import annotations

from typing import Any

from database_service import get_database_client
from object_storage_service import storage_context
from oci_helpers import get_block_client, get_compute_client, get_identity_client, get_network_client, list_all

ALWAYS_FREE_SHAPES = {"VM.Standard.E2.1.Micro", "VM.Standard.A1.Flex"}

FREE_LIMITS = {
    "a1_ocpus": 4.0,
    "a1_memory_gbs": 24.0,
    "amd_micro_count": 2,
    "block_volume_gbs": 200.0,
    "object_storage_gbs": 20.0,
    "adb_count": 2,
}


def cost_rules() -> list[dict[str, Any]]:
    return [
        {"title": "A1 Flex", "level": "good", "text": "常见 Always Free ARM 规格，但 OCPU/内存总量需要自己控制。"},
        {"title": "E2.1.Micro", "level": "good", "text": "常见 Always Free AMD 规格。"},
        {"title": "Boot Volume", "level": "warn", "text": "引导卷和块存储容量扩大后可能产生费用，删除实例时也要检查卷是否保留。"},
        {"title": "Reserved Public IP", "level": "warn", "text": "保留公网 IP、换 IP 后遗留的 IP 资源需要定期检查。"},
        {"title": "Autonomous Database", "level": "warn", "text": "创建自治数据库时确认 Free Tier 选项和资源规格。"},
        {"title": "Object Storage", "level": "warn", "text": "对象存储、出网流量、跨区域复制都可能带来额外费用。"},
    ]


def shape_cost_badge(shape: str) -> str:
    return "Always Free 常见规格" if shape in ALWAYS_FREE_SHAPES else "注意规格费用"


def _usage_item(key: str, label: str, used: float, limit: float, unit: str = "") -> dict[str, Any]:
    percent = 0 if limit <= 0 else min(999, round(used / limit * 100, 1))
    status = "ok" if used <= limit else "over"
    return {"key": key, "label": label, "used": used, "limit": limit, "unit": unit, "percent": percent, "status": status}


def free_tier_context(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    usage = {
        "a1_ocpus": 0.0,
        "a1_memory_gbs": 0.0,
        "amd_micro_count": 0.0,
        "block_volume_gbs": 0.0,
        "object_storage_gbs": 0.0,
        "adb_count": 0.0,
    }
    risks: list[dict[str, str]] = []
    compute = get_compute_client(tenant_cfg)
    identity = get_identity_client(tenant_cfg)
    block = get_block_client(tenant_cfg)

    for ad in list_all(identity.list_availability_domains, compartment_id=tenant_cfg["tenant_id"]):
        for instance in list_all(compute.list_instances, compartment_id=tenant_cfg["tenant_id"], availability_domain=ad.name):
            if getattr(instance, "lifecycle_state", "") == "TERMINATED":
                continue
            shape = getattr(instance, "shape", "")
            if shape == "VM.Standard.A1.Flex":
                shape_config = getattr(instance, "shape_config", None)
                usage["a1_ocpus"] += float(getattr(shape_config, "ocpus", 0) or 0)
                usage["a1_memory_gbs"] += float(getattr(shape_config, "memory_in_gbs", 0) or 0)
            elif shape == "VM.Standard.E2.1.Micro":
                usage["amd_micro_count"] += 1
            else:
                risks.append({"level": "warn", "text": f"实例 {getattr(instance, 'display_name', instance.id)} 使用 {shape}，不是常见 Always Free 规格。"})
        for volume in list_all(block.list_boot_volumes, compartment_id=tenant_cfg["tenant_id"], availability_domain=ad.name):
            if getattr(volume, "lifecycle_state", "") != "TERMINATED":
                usage["block_volume_gbs"] += float(getattr(volume, "size_in_gbs", 0) or 0)
        for volume in list_all(block.list_volumes, compartment_id=tenant_cfg["tenant_id"], availability_domain=ad.name):
            if getattr(volume, "lifecycle_state", "") != "TERMINATED":
                usage["block_volume_gbs"] += float(getattr(volume, "size_in_gbs", 0) or 0)

    try:
        storage = storage_context(tenant_cfg)
        usage["object_storage_gbs"] = round(float(storage["storage_stats"]["total_size"] or 0) / (1024**3), 3)
    except Exception as exc:
        risks.append({"level": "warn", "text": f"对象存储用量读取失败：{exc}"})

    try:
        db_client = get_database_client(tenant_cfg)
        for db in list_all(db_client.list_autonomous_databases, compartment_id=tenant_cfg["tenant_id"]):
            if getattr(db, "lifecycle_state", "") != "TERMINATED" and bool(getattr(db, "is_free_tier", False)):
                usage["adb_count"] += 1
            elif getattr(db, "lifecycle_state", "") != "TERMINATED":
                risks.append({"level": "warn", "text": f"自治数据库 {getattr(db, 'display_name', db.id)} 不是 Free Tier。"})
    except Exception as exc:
        risks.append({"level": "warn", "text": f"自治数据库用量读取失败：{exc}"})

    items = [
        _usage_item("a1_ocpus", "A1 OCPU", usage["a1_ocpus"], FREE_LIMITS["a1_ocpus"], "OCPU"),
        _usage_item("a1_memory_gbs", "A1 内存", usage["a1_memory_gbs"], FREE_LIMITS["a1_memory_gbs"], "GB"),
        _usage_item("amd_micro_count", "AMD Micro", usage["amd_micro_count"], FREE_LIMITS["amd_micro_count"], "台"),
        _usage_item("block_volume_gbs", "块/引导卷", usage["block_volume_gbs"], FREE_LIMITS["block_volume_gbs"], "GB"),
        _usage_item("object_storage_gbs", "对象存储", usage["object_storage_gbs"], FREE_LIMITS["object_storage_gbs"], "GB"),
        _usage_item("adb_count", "自治数据库", usage["adb_count"], FREE_LIMITS["adb_count"], "个"),
    ]
    for item in items:
        if item["status"] == "over":
            risks.append({"level": "bad", "text": f"{item['label']} 估算已超过常见 Always Free 额度。"})
    return {"free_items": items, "risks": risks}
