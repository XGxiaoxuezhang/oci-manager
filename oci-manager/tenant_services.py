from __future__ import annotations

import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import oci
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from compartment_scope import compartment_of
from oci_helpers import (
    build_dashboard_cards,
    build_security_rule,
    calc_fingerprint_from_key,
    fingerprint_from_private_pem,
    get_block_client,
    get_compute_client,
    get_identity_client,
    get_network_client,
    instance_boot_volume,
    instance_console_connections,
    list_all,
    primary_private_ip,
    primary_vnic,
    summarize_rule,
    vnic_ipv6_addresses,
)
from settings import CONSOLE_KEY_DIR, TENANT_DIR
from storage import fmt_dt, load_tenants, now_iso, save_tenants

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._@+-]{1,126}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 实例生命周期状态分类，用于 UI 状态徽章与操作显隐。
_STATE_KIND = {
    "RUNNING": "running",
    "STOPPED": "stopped",
    "TERMINATED": "terminated",
    "PROVISIONING": "pending",
    "STARTING": "pending",
    "STOPPING": "pending",
    "TERMINATING": "pending",
}

# 每种状态允许的实例操作（映射到 INSTANCE_ACTIONS 的 key）。
# 注意：terminate 不在此表中 —— 它不由 INSTANCE_ACTIONS 映射，走的是独立路由与确认流程，
# 能否终止由 instance_can_terminate() 单独判断。
_STATE_ACTIONS = {
    "RUNNING": ["softstop", "softreset", "reset"],
    "STOPPED": ["start"],
    "TERMINATED": [],
    "PROVISIONING": [],
    "STARTING": [],
    "STOPPING": [],
    "TERMINATING": [],
}

# 已终止的实例没有再次终止的意义。
_TERMINATABLE_STATES = {"RUNNING", "STOPPED", "STOPPING", "STARTING", "PROVISIONING"}


def instance_state_kind(state: str) -> str:
    """返回实例状态分类：running / stopped / terminated / pending / unknown。"""
    return _STATE_KIND.get(state, "unknown")


def instance_available_actions(state: str) -> list[str]:
    """返回该状态下可执行的实例操作 key 列表。"""
    return _STATE_ACTIONS.get(state, [])


def instance_can_terminate(state: str) -> bool:
    """返回该状态下是否允许终止实例。"""
    return state in _TERMINATABLE_STATES


def dashboard_context(tenants: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "tenants": build_dashboard_cards(tenants),
        "stats": {
            "tenant_count": len(tenants),
            "region_count": len({tenant.get("region") for tenant in tenants.values() if tenant.get("region")}),
            "latest_created": max((tenant.get("created", "") for tenant in tenants.values()), default=""),
        },
    }


def instance_overview_context(tenants: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals = {"tenants": len(tenants), "instances": 0, "running": 0}

    def fetch_one(name_cfg: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        tenant_name, cfg = name_cfg
        tenant_cfg = dict(cfg)
        tenant_cfg["_tenant_name"] = tenant_name
        try:
            instances = list_instance_rows(tenant_cfg)
            running = sum(1 for item in instances if item["state"] == "RUNNING")
            return {
                "tenant_name": tenant_name,
                "region": cfg.get("region", "-"),
                "status": "ok",
                "count": len(instances),
                "running": running,
                "instances": instances,
                "error": "",
            }
        except Exception as exc:
            return {
                "tenant_name": tenant_name,
                "region": cfg.get("region", "-"),
                "status": "error",
                "count": 0,
                "running": 0,
                "instances": [],
                "error": str(exc),
            }

    # 并行拉取各租户实例，显著降低总览页加载时间。
    items = list(tenants.items())
    if len(items) <= 1:
        fetched = [fetch_one(item) for item in items]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(len(items), 8)) as pool:
            fetched = list(pool.map(fetch_one, items))

    # 保持与传入字典一致的顺序，并汇总总数。
    for row in fetched:
        totals["instances"] += row["count"]
        totals["running"] += row["running"]
    rows.extend(fetched)
    return {"overview_rows": rows, "overview_totals": totals}


def create_tenant_record(
    tenants: dict[str, dict[str, Any]],
    tenant_name: str,
    tenant_id: str,
    user_id: str,
    region: str,
    fingerprint: str,
    key_file: Any,
) -> None:
    tenant_path = TENANT_DIR / tenant_name
    tenant_path.mkdir(parents=True, exist_ok=True)
    key_path = tenant_path / "key.pem"
    key_file.save(key_path)
    if not key_path.exists() or key_path.stat().st_size == 0:
        raise ValueError("私钥文件为空。")
    tenants[tenant_name] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "region": region,
        "fingerprint": fingerprint,
        "key_path": str(key_path),
        "created": now_iso(),
    }


def remove_tenant_record(tenants: dict[str, dict[str, Any]], tenant_name: str) -> None:
    tenants.pop(tenant_name)
    tenant_dir = TENANT_DIR / tenant_name
    if tenant_dir.exists() and tenant_dir.parent == TENANT_DIR:
        shutil.rmtree(tenant_dir, ignore_errors=True)


def list_user_rows(tenant_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    client = get_identity_client(tenant_cfg)
    configured_user_id = tenant_cfg.get("user_id", "")
    configured_fp = tenant_cfg.get("fingerprint", "")
    rows = []
    for user in list_all(client.list_users, compartment_id=tenant_cfg["tenant_id"]):
        try:
            totp_devices = list_all(client.list_mfa_totp_devices, user_id=user.id)
        except Exception:
            totp_devices = []
        api_keys: list[dict[str, Any]] = []
        try:
            for key in list_all(client.list_api_keys, user_id=user.id):
                api_keys.append(
                    {
                        "fingerprint": key.fingerprint,
                        "state": getattr(key, "lifecycle_state", "-"),
                        "created": fmt_dt(getattr(key, "time_created", None)),
                    }
                )
        except Exception:
            api_keys = []
        is_configured_user = user.id == configured_user_id
        has_unmatched_fp = bool(configured_fp) and is_configured_user and configured_fp not in {k["fingerprint"] for k in api_keys}
        rows.append(
            {
                "id": user.id,
                "name": user.name,
                "email": user.email or "-",
                "mfa": bool(user.is_mfa_activated),
                "totp_count": len(totp_devices),
                "created": fmt_dt(user.time_created),
                "is_configured_user": is_configured_user,
                "configured_fingerprint": configured_fp if is_configured_user else "",
                "api_key_count": len(api_keys),
                "api_keys": api_keys,
                "fingerprint_mismatch": has_unmatched_fp,
            }
        )
    return rows


def iam_security_context(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    """IAM 安全自检：汇总用户、MFA、API key 与指纹匹配风险。"""
    rows = list_user_rows(tenant_cfg)
    findings: list[dict[str, str]] = []
    users_without_mfa = []
    for user in rows:
        if not user["mfa"]:
            users_without_mfa.append(user["name"])
            findings.append({"level": "warn", "text": f"用户 {user['name']} 未启用 MFA。"})
        if user["fingerprint_mismatch"]:
            findings.append(
                {
                    "level": "bad",
                    "text": f"当前使用的用户 {user['name']} 的 API 密钥指纹与本地配置不一致，可能已被轮换或替换。",
                }
            )
    if not users_without_mfa:
        findings.append({"level": "good", "text": "所有用户均已启用 MFA。"})
    # 本地密钥指纹是否与配置一致
    local_fp = None
    key_path = tenant_cfg.get("key_path", "")
    if key_path:
        local_fp = calc_fingerprint_from_key(key_path)
    fp_mismatch = bool(local_fp) and bool(tenant_cfg.get("fingerprint")) and local_fp != tenant_cfg.get("fingerprint")
    if fp_mismatch:
        findings.append({"level": "bad", "text": "本地私钥反算指纹与配置 fingerprint 不一致。"})
    summary = {
        "user_count": len(rows),
        "mfa_enabled_count": sum(1 for u in rows if u["mfa"]),
        "api_key_total": sum(u["api_key_count"] for u in rows),
        "fingerprint_mismatch_count": sum(1 for u in rows if u["fingerprint_mismatch"]),
    }
    return {"users": rows, "findings": findings, "summary": summary, "local_fingerprint": local_fp}


def create_user_for_tenant(tenant_cfg: dict[str, Any], username: str, email: str, is_admin: bool, create_password: bool) -> list[str]:
    username = username.strip()
    email = parseaddr(email.strip())[1]
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("用户名需以字母开头，只能包含字母、数字、点、下划线、加号、短横线和 @。")
    if not EMAIL_RE.fullmatch(email):
        raise ValueError("邮箱格式不正确。")
    client = get_identity_client(tenant_cfg)
    messages: list[str] = []
    new_user = client.create_user(
        oci.identity.models.CreateUserDetails(
            compartment_id=tenant_cfg["tenant_id"],
            name=username,
            description="由 OCI 管理器创建",
            email=email,
        )
    ).data
    messages.append(f"用户 {username} 创建成功。")
    if create_password:
        password = client.create_or_reset_ui_password(user_id=new_user.id).data.password
        messages.append(f"临时控制台密码: {password}")
    if is_admin:
        groups = list_all(client.list_groups, compartment_id=tenant_cfg["tenant_id"])
        admin_group = next((group for group in groups if group.name.lower() in {"administrators", "admin"}), None)
        if admin_group:
            client.add_user_to_group(oci.identity.models.AddUserToGroupDetails(group_id=admin_group.id, user_id=new_user.id))
            messages.append("已加入管理员组。")
        else:
            messages.append("没有找到 administrators/admin 组，未自动授予管理员权限。")
    return messages


def reset_user_mfa(tenant_cfg: dict[str, Any], user_id: str) -> None:
    client = get_identity_client(tenant_cfg)
    for device in list_all(client.list_mfa_totp_devices, user_id=user_id):
        client.delete_mfa_totp_device(user_id=user_id, mfa_totp_device_id=device.id)


def rotate_api_key(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    """为当前配置的用户轮换 API 密钥。

    顺序很关键：先上传到 OCI，成功之后才替换本地私钥和更新配置。
    任何一步失败都不会动本地文件，避免把自己锁在门外。
    旧密钥不会自动删除 —— 留着做回退，确认新密钥能用后到用户页手动删。
    """
    tenant_name = tenant_cfg.get("_tenant_name")
    if not tenant_name:
        raise ValueError("缺少租户上下文，无法轮换密钥。")
    user_id = tenant_cfg.get("user_id", "")
    if not user_id:
        raise ValueError("租户没有配置 User OCID。")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )

    client = get_identity_client(tenant_cfg)
    client.upload_api_key(user_id, oci.identity.models.CreateApiKeyDetails(key=public_bytes.decode("utf-8")))
    fingerprint = fingerprint_from_private_pem(private_bytes)
    if not fingerprint:
        raise ValueError("新密钥指纹计算失败，已上传但本地未切换，请到 OCI 控制台删除该密钥。")

    key_path = Path(tenant_cfg.get("key_path") or (TENANT_DIR / tenant_name / "key.pem"))
    key_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if key_path.exists():
        backup_path = key_path.with_name(key_path.name + ".bak")
        shutil.copy2(key_path, backup_path)
    key_path.write_bytes(private_bytes)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass

    tenants = load_tenants()
    if tenant_name in tenants:
        tenants[tenant_name]["key_path"] = str(key_path)
        tenants[tenant_name]["fingerprint"] = fingerprint
        tenants[tenant_name]["key_rotated"] = now_iso()
        save_tenants(tenants)

    return {
        "fingerprint": fingerprint,
        "key_path": str(key_path),
        "backup_path": str(backup_path) if backup_path else "",
        "old_fingerprint": tenant_cfg.get("fingerprint", ""),
    }


def delete_api_key(tenant_cfg: dict[str, Any], user_id: str, fingerprint: str) -> None:
    """删除指定用户的某个 API Key。拒绝删除当前正在使用的那把，避免自锁。"""
    fingerprint = (fingerprint or "").strip()
    if not fingerprint:
        raise ValueError("缺少指纹。")
    if fingerprint == (tenant_cfg.get("fingerprint") or "").strip():
        raise ValueError("这是当前正在使用的密钥，直接删除会让本平台无法访问 OCI。请先轮换再删除旧密钥。")
    get_identity_client(tenant_cfg).delete_api_key(user_id, fingerprint)


def _instance_row(tenant_cfg: dict[str, Any], ad_name: str, instance: Any) -> dict[str, Any]:
    """组装一行实例数据。每次新建客户端，便于在线程池里并发调用。"""
    network_client = get_network_client(tenant_cfg)
    compute_client = get_compute_client(tenant_cfg)
    vnic = primary_vnic(network_client, compute_client, compartment_of(tenant_cfg), instance.id)
    state = instance.lifecycle_state
    return {
        "id": instance.id,
        "name": instance.display_name,
        "shape": instance.shape,
        "state": state,
        "state_kind": instance_state_kind(state),
        "actions": instance_available_actions(state),
        "can_terminate": instance_can_terminate(state),
        "availability_domain": ad_name,
        "public_ip": getattr(vnic, "public_ip", None) or "-",
        "private_ip": getattr(vnic, "private_ip", None) or "-",
        "ipv6": ", ".join(vnic_ipv6_addresses(network_client, vnic)) or "-",
        "created": fmt_dt(instance.time_created),
    }


def list_instance_rows(tenant_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """列出当前 compartment 下的实例。

    每台实例原本要串行发 2~3 次请求（vnic attachment / get_vnic / list_ipv6s），
    10 台就是 30 次往返，这是实例页要等 2~17 秒的真正原因。这里把「按 AD 拉实例」
    和「逐台补网卡信息」两层都并发掉，耗时降到最慢那一次往返的量级。

    结果挂 TTL 缓存（cache.py），写操作路由调 invalidate_tenant() 后失效。
    """
    from cache import cached

    tenant_name = tenant_cfg.get("_tenant_name") or tenant_cfg.get("tenant_id", "-")

    def loader() -> list[dict[str, Any]]:
        from monitoring_service import attach_metrics_to_rows

        rows = _list_instance_rows_uncached(tenant_cfg)
        try:
            attach_metrics_to_rows(rows, tenant_cfg)
        except Exception:
            pass  # 监控数据缺失不影响列表
        return rows

    try:
        from flask import g

        bypass = bool(getattr(g, "cache_bypass", False))
    except RuntimeError:
        bypass = False
    return cached(("instances", tenant_name, compartment_of(tenant_cfg)), loader, bypass=bypass)


def _list_instance_rows_uncached(tenant_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    compute_client = get_compute_client(tenant_cfg)
    identity_client = get_identity_client(tenant_cfg)
    compartment = compartment_of(tenant_cfg)
    ads = list_all(identity_client.list_availability_domains, compartment_id=tenant_cfg["tenant_id"])

    pairs: list[tuple[str, Any]] = []
    if ads:
        with ThreadPoolExecutor(max_workers=min(8, len(ads))) as pool:
            futures = {
                pool.submit(
                    list_all,
                    compute_client.list_instances,
                    compartment_id=compartment,
                    availability_domain=ad.name,
                ): ad.name
                for ad in ads
            }
            for future in as_completed(futures):
                ad_name = futures[future]
                try:
                    instances = future.result()
                except Exception:
                    continue
                for instance in instances:
                    pairs.append((ad_name, instance))
    pairs.sort(key=lambda item: (item[0], getattr(item[1], "display_name", "") or ""))

    rows: list[dict[str, Any]] = []
    if pairs:
        with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as pool:
            futures = [pool.submit(_instance_row, tenant_cfg, ad_name, instance) for ad_name, instance in pairs]
            for future in futures:
                try:
                    rows.append(future.result())
                except Exception:
                    continue
    rows.sort(key=lambda item: (item["state"], item["name"].lower()))
    return rows


def replace_public_ip(tenant_cfg: dict[str, Any], instance_id: str, release_old: bool = True) -> tuple[str, dict[str, Any]]:
    """为实例重新分配公网 IP。

    旧 IP 默认是「保留」(RESERVED) 类型，解绑后如果留着不管会一直堆在账号里占用配额，
    所以默认顺手释放；释放失败（例如仍处于 UNASSIGNING）不阻断换 IP，只回报给上层。
    """
    compute_client = get_compute_client(tenant_cfg)
    network_client = get_network_client(tenant_cfg)
    instance = compute_client.get_instance(instance_id).data
    vnic = primary_vnic(network_client, compute_client, compartment_of(tenant_cfg), instance_id)
    if vnic is None:
        raise ValueError("没有找到实例主网卡。")
    private_ip = primary_private_ip(network_client, vnic.id)
    if private_ip is None:
        raise ValueError("没有找到主私网 IP。")
    released: list[str] = []
    release_error = ""
    if vnic.public_ip:
        reserved_ips = list_all(network_client.list_public_ips, scope="REGION", compartment_id=compartment_of(tenant_cfg), lifetime="RESERVED")
        current_ip = next((item for item in reserved_ips if getattr(item, "assigned_entity_id", None) == private_ip.id or item.ip_address == vnic.public_ip), None)
        if current_ip:
            network_client.update_public_ip(current_ip.id, oci.core.models.UpdatePublicIpDetails(private_ip_id=None))
            if release_old:
                try:
                    network_client.delete_public_ip(current_ip.id)
                    released.append(current_ip.ip_address)
                except Exception as exc:
                    release_error = f"旧 IP {current_ip.ip_address} 已解绑但释放失败，可在公网 IP 页手动释放：{exc}"
    new_ip = network_client.create_public_ip(
        oci.core.models.CreatePublicIpDetails(
            compartment_id=compartment_of(tenant_cfg),
            display_name=f"IP_{instance.display_name}_{now_iso().replace(':', '').replace('-', '')}",
            lifetime="RESERVED",
            private_ip_id=private_ip.id,
        )
    ).data
    return new_ip.ip_address, {
        "id": instance_id,
        "name": instance.display_name,
        "public_ip": vnic.public_ip or "-",
        "released": released,
        "release_error": release_error,
    }


def list_public_ip_rows(tenant_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """列出租户下所有保留公网 IP，未挂载的即为可释放的孤儿 IP。"""
    network_client = get_network_client(tenant_cfg)
    rows: list[dict[str, Any]] = []
    for item in list_all(network_client.list_public_ips, scope="REGION", compartment_id=compartment_of(tenant_cfg), lifetime="RESERVED"):
        assigned_id = getattr(item, "assigned_entity_id", None) or ""
        rows.append(
            {
                "id": item.id,
                "ip_address": getattr(item, "ip_address", "-"),
                "display_name": getattr(item, "display_name", "") or "-",
                "state": getattr(item, "lifecycle_state", "-"),
                "attached": bool(assigned_id),
                "assigned_entity_type": getattr(item, "assigned_entity_type", "") or "-",
                "assigned_entity_id": assigned_id or "-",
                "created": fmt_dt(getattr(item, "time_created", None)),
            }
        )
    rows.sort(key=lambda row: (row["attached"], row["ip_address"]))
    return rows


def release_public_ip(tenant_cfg: dict[str, Any], public_ip_id: str) -> str:
    """释放指定的保留公网 IP，仅允许释放未挂载的。"""
    network_client = get_network_client(tenant_cfg)
    public_ip = network_client.get_public_ip(public_ip_id).data
    if getattr(public_ip, "assigned_entity_id", None):
        raise ValueError(f"{public_ip.ip_address} 仍挂在资源上，请先解绑再释放。")
    network_client.delete_public_ip(public_ip_id)
    return public_ip.ip_address


def change_ip_context(tenant_cfg: dict[str, Any], instance_id: str) -> dict[str, Any]:
    compute_client = get_compute_client(tenant_cfg)
    network_client = get_network_client(tenant_cfg)
    instance = compute_client.get_instance(instance_id).data
    vnic = primary_vnic(network_client, compute_client, compartment_of(tenant_cfg), instance_id)
    if vnic is None:
        raise ValueError("没有找到实例主网卡。")
    return {"id": instance_id, "name": instance.display_name, "public_ip": vnic.public_ip or "-"}


def instance_edit_context(tenant_cfg: dict[str, Any], instance_id: str) -> dict[str, Any]:
    compute_client = get_compute_client(tenant_cfg)
    instance = compute_client.get_instance(instance_id).data
    shape = instance.shape
    shape_config = getattr(instance, "shape_config", None)
    current_ocpus = getattr(shape_config, "ocpus", None)
    current_memory_gbs = getattr(shape_config, "memory_in_gbs", None)
    is_flex_shape = str(shape or "").endswith(".Flex")
    shape_limits = None
    if is_flex_shape:
        try:
            shapes = compute_client.list_shapes(
                compartment_id=compartment_of(tenant_cfg),
                availability_domain=instance.availability_domain,
                shape=shape,
            ).data
        except Exception:
            shapes = []
        if shapes:
            item = shapes[0]
            ocpu_options = getattr(item, "ocpu_options", None)
            memory_options = getattr(item, "memory_options", None)
            shape_limits = {
                "ocpu_min": getattr(ocpu_options, "min", None),
                "ocpu_max": getattr(ocpu_options, "max", None),
                "memory_min": getattr(memory_options, "min_in_g_bs", None) or getattr(memory_options, "min_in_gbs", None),
                "memory_max": getattr(memory_options, "max_in_g_bs", None) or getattr(memory_options, "max_in_gbs", None),
                "memory_per_ocpu_min": getattr(memory_options, "min_per_ocpu_in_gbs", None),
                "memory_per_ocpu_max": getattr(memory_options, "max_per_ocpu_in_gbs", None),
            }
    return {
        "instance": {
            "id": instance.id,
            "name": instance.display_name,
            "shape": shape,
            "state": instance.lifecycle_state,
            "availability_domain": instance.availability_domain,
            "current_ocpus": current_ocpus,
            "current_memory_gbs": current_memory_gbs,
            "is_flex_shape": is_flex_shape,
            "shape_limits": shape_limits,
        }
    }


def update_instance_configuration(
    tenant_cfg: dict[str, Any],
    instance_id: str,
    display_name: str,
    ocpus_raw: str,
    memory_gbs_raw: str,
) -> dict[str, Any]:
    compute_client = get_compute_client(tenant_cfg)
    instance = compute_client.get_instance(instance_id).data
    current_name = getattr(instance, "display_name", "") or ""
    shape = instance.shape
    shape_config = getattr(instance, "shape_config", None)
    current_ocpus = float(getattr(shape_config, "ocpus", 0) or 0) if shape_config else None
    current_memory_gbs = float(getattr(shape_config, "memory_in_gbs", 0) or 0) if shape_config else None
    new_name = (display_name or "").strip() or current_name
    details = oci.core.models.UpdateInstanceDetails()
    changed = []
    if new_name != current_name:
        details.display_name = new_name
        changed.append("name")

    is_flex_shape = str(shape or "").endswith(".Flex")
    if ocpus_raw.strip() or memory_gbs_raw.strip():
        if not is_flex_shape:
            raise ValueError("当前实例不是 Flex 规格，不能修改 OCPU 或内存。")
        if instance.lifecycle_state != "STOPPED":
            raise ValueError("修改 OCPU 或内存前需要先停止实例。")
        try:
            new_ocpus = float(ocpus_raw.strip())
            new_memory_gbs = float(memory_gbs_raw.strip())
        except ValueError as exc:
            raise ValueError("OCPU 和内存必须是数字。") from exc
        shapes = compute_client.list_shapes(
            compartment_id=compartment_of(tenant_cfg),
            availability_domain=instance.availability_domain,
            shape=shape,
        ).data
        if not shapes:
            raise ValueError("没有读取到当前规格的限制信息。")
        item = shapes[0]
        ocpu_options = getattr(item, "ocpu_options", None)
        memory_options = getattr(item, "memory_options", None)
        ocpu_min = float(getattr(ocpu_options, "min", 0) or 0)
        ocpu_max = float(getattr(ocpu_options, "max", 0) or 0)
        memory_min = float(getattr(memory_options, "min_in_g_bs", None) or getattr(memory_options, "min_in_gbs", 0) or 0)
        memory_max = float(getattr(memory_options, "max_in_g_bs", None) or getattr(memory_options, "max_in_gbs", 0) or 0)
        memory_per_ocpu_min = float(getattr(memory_options, "min_per_ocpu_in_gbs", 0) or 0)
        memory_per_ocpu_max = float(getattr(memory_options, "max_per_ocpu_in_gbs", 0) or 0)
        if not (ocpu_min <= new_ocpus <= ocpu_max):
            raise ValueError(f"OCPU 必须在 {ocpu_min:g} 到 {ocpu_max:g} 之间。")
        if not (memory_min <= new_memory_gbs <= memory_max):
            raise ValueError(f"内存必须在 {memory_min:g} 到 {memory_max:g} GB 之间。")
        if new_ocpus > 0:
            memory_per_ocpu = new_memory_gbs / new_ocpus
            if not (memory_per_ocpu_min <= memory_per_ocpu <= memory_per_ocpu_max):
                raise ValueError(f"每个 OCPU 的内存必须在 {memory_per_ocpu_min:g} 到 {memory_per_ocpu_max:g} GB 之间。")
        if current_ocpus != new_ocpus or current_memory_gbs != new_memory_gbs:
            details.shape_config = oci.core.models.UpdateInstanceShapeConfigDetails(
                ocpus=new_ocpus,
                memory_in_gbs=new_memory_gbs,
            )
            changed.append("shape_config")

    if not changed:
        raise ValueError("没有检测到可提交的变更。")
    compute_client.update_instance(instance_id, details)
    return {"instance_name": new_name, "changed": changed}


def rescue_context(tenant_cfg: dict[str, Any], instance_id: str) -> dict[str, Any]:
    compute_client = get_compute_client(tenant_cfg)
    network_client = get_network_client(tenant_cfg)
    instance = compute_client.get_instance(instance_id).data
    vnic = primary_vnic(network_client, compute_client, compartment_of(tenant_cfg), instance_id)
    attachment, boot_volume = instance_boot_volume(tenant_cfg, instance)
    return {
        "instance": {
            "id": instance.id,
            "name": instance.display_name,
            "shape": instance.shape,
            "state": instance.lifecycle_state,
            "availability_domain": instance.availability_domain,
            "public_ip": getattr(vnic, "public_ip", None) or "-",
            "private_ip": getattr(vnic, "private_ip", None) or "-",
            "ipv6": ", ".join(vnic_ipv6_addresses(network_client, vnic)) or "-",
            "created": fmt_dt(instance.time_created),
        },
        "boot_volume": {
            "id": getattr(boot_volume, "id", ""),
            "size_in_gbs": getattr(boot_volume, "size_in_gbs", None),
            "vpus_per_gb": getattr(boot_volume, "vpus_per_gb", None),
            "state": getattr(boot_volume, "lifecycle_state", "-"),
            "attachment_id": getattr(attachment, "id", ""),
        }
        if boot_volume
        else None,
        "consoles": instance_console_connections(tenant_cfg, instance_id),
    }


def ensure_console_key_pair() -> tuple[str, str]:
    private_path = CONSOLE_KEY_DIR / "console_key.pem"
    public_path = CONSOLE_KEY_DIR / "console_key.pub"
    if private_path.exists() and public_path.exists():
        return private_path.read_text(encoding="utf-8").strip(), public_path.read_text(encoding="utf-8").strip()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    private_path.write_bytes(private_bytes)
    public_path.write_bytes(public_bytes + b"\n")
    return private_bytes.decode("utf-8").strip(), public_bytes.decode("utf-8").strip()


def console_private_key_path() -> str:
    return str((CONSOLE_KEY_DIR / "console_key.pem").resolve())


def capture_console_history(tenant_cfg: dict[str, Any], instance_id: str) -> dict[str, Any]:
    """触发抓取实例启动/控制台日志（异步操作，立即返回）。"""
    compute_client = get_compute_client(tenant_cfg)
    history = compute_client.capture_console_history(
        oci.core.models.CaptureConsoleHistoryDetails(instance_id=instance_id)
    ).data
    return {"id": history.id, "state": history.lifecycle_state}


def list_console_history_rows(tenant_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """列出本 compartment 的控制台历史记录。"""
    compute_client = get_compute_client(tenant_cfg)
    rows: list[dict[str, Any]] = []
    for item in list_all(compute_client.list_console_histories, compartment_id=compartment_of(tenant_cfg)):
        rows.append(
            {
                "id": item.id,
                "instance_id": item.instance_id,
                "state": item.lifecycle_state,
                "created": fmt_dt(item.time_created),
                "compartment_id": item.compartment_id,
            }
        )
    rows.sort(key=lambda row: row["created"], reverse=True)
    return rows


def console_history_content(tenant_cfg: dict[str, Any], history_id: str) -> tuple[str, str]:
    """获取某条历史的日志内容。返回 (state, content)。"""
    compute_client = get_compute_client(tenant_cfg)
    state = compute_client.get_console_history(history_id).data.lifecycle_state
    if state != "SUCCEEDED":
        return state, ""
    response = compute_client.get_console_history_content(history_id)
    content = (response.data or "").decode("utf-8", errors="replace")
    return state, content[-8000:]  # 只保留尾部（启动日志开头是 OVM 固件输出，价值低）


def create_console(tenant_cfg: dict[str, Any], instance_id: str, public_key: str = "") -> tuple[str, bool]:
    generated = False
    if not public_key.strip():
        _, public_key = ensure_console_key_pair()
        generated = True
    connection = get_compute_client(tenant_cfg).create_instance_console_connection(
        oci.core.models.CreateInstanceConsoleConnectionDetails(instance_id=instance_id, public_key=public_key)
    ).data
    return connection.id, generated


def expand_boot_volume(tenant_cfg: dict[str, Any], instance_id: str, new_size: int) -> None:
    instance = get_compute_client(tenant_cfg).get_instance(instance_id).data
    _, boot_volume = instance_boot_volume(tenant_cfg, instance)
    if not boot_volume:
        raise ValueError("没有找到引导卷。")
    if new_size <= int(getattr(boot_volume, "size_in_gbs", 0) or 0):
        raise ValueError("新容量必须大于当前容量。")
    get_block_client(tenant_cfg).update_boot_volume(boot_volume.id, oci.core.models.UpdateBootVolumeDetails(size_in_gbs=new_size))


def terminate_instance(tenant_cfg: dict[str, Any], instance_id: str, confirm_name: str, preserve_boot_volume: bool) -> str:
    compute_client = get_compute_client(tenant_cfg)
    instance = compute_client.get_instance(instance_id).data
    instance_name = getattr(instance, "display_name", "") or instance_id
    if confirm_name.strip() != instance_name:
        raise ValueError("确认名称不匹配，未终止实例。")
    compute_client.terminate_instance(instance_id, preserve_boot_volume=preserve_boot_volume)
    return instance_name


def list_security_list_rows(tenant_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    security_lists = list_all(get_network_client(tenant_cfg).list_security_lists, compartment_id=compartment_of(tenant_cfg))
    return [
        {
            "id": item.id,
            "name": item.display_name,
            "vcn_id": item.vcn_id,
            "ingress_count": len(getattr(item, "ingress_security_rules", []) or []),
            "egress_count": len(getattr(item, "egress_security_rules", []) or []),
            "created": fmt_dt(getattr(item, "time_created", None)),
        }
        for item in security_lists
    ]


def security_rules_context(tenant_cfg: dict[str, Any], security_list_id: str) -> dict[str, Any]:
    security_list = get_network_client(tenant_cfg).get_security_list(security_list_id).data
    return {
        "security_list": {"id": security_list_id, "name": security_list.display_name},
        "ingress_rules": [summarize_rule("ingress", index, rule) for index, rule in enumerate(getattr(security_list, "ingress_security_rules", []) or [])],
        "egress_rules": [summarize_rule("egress", index, rule) for index, rule in enumerate(getattr(security_list, "egress_security_rules", []) or [])],
    }


def append_security_rule(tenant_cfg: dict[str, Any], security_list_id: str, rule_type: str, protocol: str, source_dest: str, port_min: str, port_max: str, description: str) -> None:
    if rule_type not in {"ingress", "egress"}:
        raise ValueError("规则方向不正确。")
    client = get_network_client(tenant_cfg)
    security_list = client.get_security_list(security_list_id).data
    new_rule = build_security_rule(rule_type, protocol, source_dest, port_min, port_max, description)
    ingress_rules = list(getattr(security_list, "ingress_security_rules", []) or [])
    egress_rules = list(getattr(security_list, "egress_security_rules", []) or [])
    if rule_type == "ingress":
        ingress_rules.append(new_rule)
    else:
        egress_rules.append(new_rule)
    client.update_security_list(
        security_list_id,
        oci.core.models.UpdateSecurityListDetails(
            display_name=security_list.display_name,
            ingress_security_rules=ingress_rules,
            egress_security_rules=egress_rules,
        ),
    )


def remove_security_rule(tenant_cfg: dict[str, Any], security_list_id: str, rule_type: str, rule_index: int) -> None:
    if rule_type not in {"ingress", "egress"}:
        raise ValueError("规则方向不正确。")
    client = get_network_client(tenant_cfg)
    security_list = client.get_security_list(security_list_id).data
    ingress_rules = list(getattr(security_list, "ingress_security_rules", []) or [])
    egress_rules = list(getattr(security_list, "egress_security_rules", []) or [])
    if rule_type == "ingress":
        if rule_index < 0 or rule_index >= len(ingress_rules):
            raise ValueError("入站规则序号不存在。")
        ingress_rules.pop(rule_index)
    else:
        if rule_index < 0 or rule_index >= len(egress_rules):
            raise ValueError("出站规则序号不存在。")
        egress_rules.pop(rule_index)
    client.update_security_list(
        security_list_id,
        oci.core.models.UpdateSecurityListDetails(
            display_name=security_list.display_name,
            ingress_security_rules=ingress_rules,
            egress_security_rules=egress_rules,
        ),
    )
