from __future__ import annotations

import ipaddress
import re
from typing import Any

import oci

from oci_helpers import build_config, client_kwargs, list_all
from storage import fmt_dt


def get_database_client(tenant_cfg: dict[str, str]) -> oci.database.DatabaseClient:
    return oci.database.DatabaseClient(build_config(tenant_cfg), **client_kwargs())


def _connection_rows(connection_strings: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not connection_strings:
        return rows
    profile_map = getattr(connection_strings, "profiles", None)
    if isinstance(profile_map, dict):
        for name, value in profile_map.items():
            rows.append({"name": name, "value": value})
    elif isinstance(profile_map, list):
        for item in profile_map:
            rows.append(
                {
                    "name": getattr(item, "display_name", None) or getattr(item, "consumer_group", "-"),
                    "value": getattr(item, "value", ""),
                }
            )
    high = getattr(connection_strings, "high", None)
    medium = getattr(connection_strings, "medium", None)
    low = getattr(connection_strings, "low", None)
    tp = getattr(connection_strings, "tp", None)
    tpurgent = getattr(connection_strings, "tpurgent", None)
    for name, value in [("high", high), ("medium", medium), ("low", low), ("tp", tp), ("tpurgent", tpurgent)]:
        if value and not any(item["name"].lower() == name for item in rows):
            rows.append({"name": name, "value": value})
    return rows


def _db_row(db: Any) -> dict[str, Any]:
    return {
        "id": db.id,
        "name": db.db_name,
        "display_name": db.display_name,
        "workload": getattr(db, "db_workload", "-"),
        "cpu_core_count": getattr(db, "cpu_core_count", "-"),
        "data_storage_size_in_tbs": getattr(db, "data_storage_size_in_tbs", "-"),
        "lifecycle_state": getattr(db, "lifecycle_state", "-"),
        "is_free_tier": bool(getattr(db, "is_free_tier", False)),
        "created": fmt_dt(getattr(db, "time_created", None)),
    }


def _db_detail(db: Any) -> dict[str, Any]:
    return {
        **_db_row(db),
        "subnet_id": getattr(db, "subnet_id", "-"),
        "private_endpoint": getattr(db, "private_endpoint", "-"),
        "private_endpoint_label": getattr(db, "private_endpoint_label", "-"),
        "license_model": getattr(db, "license_model", "-"),
        "db_version": getattr(db, "db_version", "-"),
        "is_mtls_connection_required": bool(getattr(db, "is_mtls_connection_required", False)),
        "whitelisted_ips": getattr(db, "whitelisted_ips", []) or [],
        "connection_strings": _connection_rows(getattr(db, "connection_strings", None) or getattr(db, "all_connection_strings", None)),
    }


def list_autonomous_databases_context(tenant_cfg: dict[str, str], database_id: str | None = None) -> dict[str, Any]:
    client = get_database_client(tenant_cfg)
    rows = [_db_row(db) for db in list_all(client.list_autonomous_databases, compartment_id=tenant_cfg["tenant_id"])]
    rows.sort(key=lambda item: item["display_name"].lower())
    selected_database = None
    backups: list[dict[str, Any]] = []
    if database_id:
        db = client.get_autonomous_database(database_id).data
        selected_database = _db_detail(db)
        backup_rows = list_all(client.list_autonomous_database_backups, autonomous_database_id=database_id)
        for backup in sorted(backup_rows, key=lambda item: getattr(item, "time_ended", None) or getattr(item, "time_started", None), reverse=True)[:10]:
            backups.append(
                {
                    "display_name": getattr(backup, "display_name", "-"),
                    "type": getattr(backup, "type", "-"),
                    "status": getattr(backup, "lifecycle_state", "-"),
                    "start": fmt_dt(getattr(backup, "time_started", None)),
                    "end": fmt_dt(getattr(backup, "time_ended", None)),
                }
            )
    stats = {
        "total": len(rows),
        "available": sum(1 for row in rows if row["lifecycle_state"] == "AVAILABLE"),
        "free_tier": sum(1 for row in rows if row["is_free_tier"]),
    }
    return {
        "autonomous_databases": rows,
        "selected_database": selected_database,
        "backups": backups,
        "database_stats": stats,
        "database_form_defaults": {
            "workload": "OLTP",
            "db_version": "19c",
            "cpu_core_count": 1,
            "storage_size_gbs": 20,
            "character_set": "AL32UTF8",
            "ncharacter_set": "AL16UTF16",
            "is_mtls_connection_required": True,
            "is_auto_scaling_enabled": False,
        },
    }


def start_autonomous_database(tenant_cfg: dict[str, str], database_id: str) -> None:
    get_database_client(tenant_cfg).start_autonomous_database(database_id)


def stop_autonomous_database(tenant_cfg: dict[str, str], database_id: str) -> None:
    get_database_client(tenant_cfg).stop_autonomous_database(database_id)


def delete_autonomous_database(tenant_cfg: dict[str, str], database_id: str) -> None:
    get_database_client(tenant_cfg).delete_autonomous_database(database_id)


def validate_create_form(
    display_name: str,
    db_name: str,
    admin_password: str,
    cpu_core_count: int,
    storage_size_gbs: int,
    whitelisted_ips: list[str],
    subnet_id: str,
    private_endpoint_label: str,
) -> dict[str, Any]:
    cleaned_name = db_name.upper().strip()
    if not display_name:
        raise ValueError("显示名称不能为空。")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,13}", cleaned_name):
        raise ValueError("数据库名必须以字母开头，只能包含字母和数字，且最长 14 位。")
    if len(admin_password) < 12:
        raise ValueError("管理员密码至少 12 位。")
    rules = [
        (r"[A-Z]", "大写字母"),
        (r"[a-z]", "小写字母"),
        (r"[0-9]", "数字"),
        (r"[^A-Za-z0-9]", "特殊字符"),
    ]
    missing = [label for pattern, label in rules if re.search(pattern, admin_password) is None]
    if missing:
        raise ValueError(f"管理员密码还缺少：{'、'.join(missing)}。")
    if cpu_core_count < 1:
        raise ValueError("CPU 核数至少为 1。")
    if storage_size_gbs < 20:
        raise ValueError("存储大小至少为 20 GB。")
    cleaned_ips: list[str] = []
    for item in whitelisted_ips:
        ipaddress.ip_network(item, strict=False)
        cleaned_ips.append(item)
    normalized_label = private_endpoint_label.strip().lower()
    if subnet_id and not normalized_label:
        normalized_label = cleaned_name[:12].lower()
    if normalized_label and not re.fullmatch(r"[a-z][a-z0-9-]{0,14}", normalized_label):
        raise ValueError("私网端点标签必须以小写字母开头，只能包含小写字母、数字和短横线，且最长 15 位。")
    return {
        "db_name": cleaned_name,
        "whitelisted_ips": cleaned_ips,
        "private_endpoint_label": normalized_label,
    }


def create_autonomous_database(
    tenant_cfg: dict[str, str],
    display_name: str,
    db_name: str,
    admin_password: str,
    workload: str,
    db_version: str,
    cpu_core_count: int,
    storage_size_gbs: int,
    is_free_tier: bool,
    subnet_id: str = "",
    whitelisted_ips: list[str] | None = None,
    character_set: str = "AL32UTF8",
    ncharacter_set: str = "AL16UTF16",
    is_mtls_connection_required: bool = True,
    is_auto_scaling_enabled: bool = False,
    private_endpoint_label: str = "",
) -> dict[str, Any]:
    normalized = validate_create_form(
        display_name,
        db_name,
        admin_password,
        cpu_core_count,
        storage_size_gbs,
        whitelisted_ips or [],
        subnet_id,
        private_endpoint_label,
    )
    final_cpu = 1 if is_free_tier else cpu_core_count
    final_storage = 20 if is_free_tier else storage_size_gbs
    details = oci.database.models.CreateAutonomousDatabaseDetails(
        compartment_id=tenant_cfg["tenant_id"],
        display_name=display_name,
        db_name=normalized["db_name"],
        admin_password=admin_password,
        db_workload=workload,
        db_version=db_version,
        license_model="LICENSE_INCLUDED",
        cpu_core_count=final_cpu,
        data_storage_size_in_gbs=final_storage,
        is_free_tier=is_free_tier,
        whitelisted_ips=normalized["whitelisted_ips"],
        subnet_id=subnet_id or None,
        private_endpoint_label=normalized["private_endpoint_label"] or None,
        character_set=character_set,
        ncharacter_set=ncharacter_set,
        is_mtls_connection_required=is_mtls_connection_required,
        is_auto_scaling_enabled=False if is_free_tier else is_auto_scaling_enabled,
    )
    response = get_database_client(tenant_cfg).create_autonomous_database(details)
    data = response.data
    return {
        "id": getattr(data, "id", ""),
        "lifecycle_state": getattr(data, "lifecycle_state", ""),
        "display_name": display_name,
        "cpu_core_count": final_cpu,
        "storage_size_gbs": final_storage,
        "is_free_tier": is_free_tier,
    }


def generate_wallet(tenant_cfg: dict[str, str], database_id: str, wallet_password: str) -> bytes:
    if len(wallet_password) < 8:
        raise ValueError("Wallet 密码至少 8 位。")
    response = get_database_client(tenant_cfg).generate_autonomous_database_wallet(
        database_id,
        oci.database.models.GenerateAutonomousDatabaseWalletDetails(password=wallet_password, generate_type="SINGLE"),
    )
    return response.data.content