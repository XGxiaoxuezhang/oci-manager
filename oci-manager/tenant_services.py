from __future__ import annotations

import re
import shutil
from email.utils import parseaddr
from typing import Any

import oci
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from oci_helpers import (
    build_dashboard_cards,
    build_security_rule,
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
from storage import fmt_dt, now_iso

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._@+-]{1,126}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    for tenant_name, cfg in tenants.items():
        tenant_cfg = dict(cfg)
        tenant_cfg["_tenant_name"] = tenant_name
        try:
            instances = list_instance_rows(tenant_cfg)
            running = sum(1 for item in instances if item["state"] == "RUNNING")
            totals["instances"] += len(instances)
            totals["running"] += running
            rows.append(
                {
                    "tenant_name": tenant_name,
                    "region": cfg.get("region", "-"),
                    "status": "ok",
                    "count": len(instances),
                    "running": running,
                    "instances": instances,
                    "error": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "tenant_name": tenant_name,
                    "region": cfg.get("region", "-"),
                    "status": "error",
                    "count": 0,
                    "running": 0,
                    "instances": [],
                    "error": str(exc),
                }
            )
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
    rows = []
    for user in list_all(client.list_users, compartment_id=tenant_cfg["tenant_id"]):
        try:
            totp_devices = list_all(client.list_mfa_totp_devices, user_id=user.id)
        except Exception:
            totp_devices = []
        rows.append(
            {
                "id": user.id,
                "name": user.name,
                "email": user.email or "-",
                "mfa": bool(user.is_mfa_activated),
                "totp_count": len(totp_devices),
                "created": fmt_dt(user.time_created),
            }
        )
    return rows


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


def list_instance_rows(tenant_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    compute_client = get_compute_client(tenant_cfg)
    identity_client = get_identity_client(tenant_cfg)
    network_client = get_network_client(tenant_cfg)
    rows = []
    for ad in list_all(identity_client.list_availability_domains, compartment_id=tenant_cfg["tenant_id"]):
        instances = list_all(compute_client.list_instances, compartment_id=tenant_cfg["tenant_id"], availability_domain=ad.name)
        for instance in instances:
            vnic = primary_vnic(network_client, compute_client, tenant_cfg["tenant_id"], instance.id)
            rows.append(
                {
                    "id": instance.id,
                    "name": instance.display_name,
                    "shape": instance.shape,
                    "state": instance.lifecycle_state,
                    "availability_domain": ad.name,
                    "public_ip": getattr(vnic, "public_ip", None) or "-",
                    "private_ip": getattr(vnic, "private_ip", None) or "-",
                    "ipv6": ", ".join(vnic_ipv6_addresses(network_client, vnic)) or "-",
                    "created": fmt_dt(instance.time_created),
                }
            )
    rows.sort(key=lambda item: (item["state"], item["name"].lower()))
    return rows


def replace_public_ip(tenant_cfg: dict[str, Any], instance_id: str) -> tuple[str, dict[str, Any]]:
    compute_client = get_compute_client(tenant_cfg)
    network_client = get_network_client(tenant_cfg)
    instance = compute_client.get_instance(instance_id).data
    vnic = primary_vnic(network_client, compute_client, tenant_cfg["tenant_id"], instance_id)
    if vnic is None:
        raise ValueError("没有找到实例主网卡。")
    private_ip = primary_private_ip(network_client, vnic.id)
    if private_ip is None:
        raise ValueError("没有找到主私网 IP。")
    if vnic.public_ip:
        reserved_ips = list_all(network_client.list_public_ips, scope="REGION", compartment_id=tenant_cfg["tenant_id"], lifetime="RESERVED")
        current_ip = next((item for item in reserved_ips if getattr(item, "assigned_entity_id", None) == private_ip.id or item.ip_address == vnic.public_ip), None)
        if current_ip:
            network_client.update_public_ip(current_ip.id, oci.core.models.UpdatePublicIpDetails(private_ip_id=None))
    new_ip = network_client.create_public_ip(
        oci.core.models.CreatePublicIpDetails(
            compartment_id=tenant_cfg["tenant_id"],
            display_name=f"IP_{instance.display_name}_{now_iso().replace(':', '').replace('-', '')}",
            lifetime="RESERVED",
            private_ip_id=private_ip.id,
        )
    ).data
    return new_ip.ip_address, {"id": instance_id, "name": instance.display_name, "public_ip": vnic.public_ip or "-"}


def change_ip_context(tenant_cfg: dict[str, Any], instance_id: str) -> dict[str, Any]:
    compute_client = get_compute_client(tenant_cfg)
    network_client = get_network_client(tenant_cfg)
    instance = compute_client.get_instance(instance_id).data
    vnic = primary_vnic(network_client, compute_client, tenant_cfg["tenant_id"], instance_id)
    if vnic is None:
        raise ValueError("没有找到实例主网卡。")
    return {"id": instance_id, "name": instance.display_name, "public_ip": vnic.public_ip or "-"}


def rescue_context(tenant_cfg: dict[str, Any], instance_id: str) -> dict[str, Any]:
    compute_client = get_compute_client(tenant_cfg)
    network_client = get_network_client(tenant_cfg)
    instance = compute_client.get_instance(instance_id).data
    vnic = primary_vnic(network_client, compute_client, tenant_cfg["tenant_id"], instance_id)
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
    security_lists = list_all(get_network_client(tenant_cfg).list_security_lists, compartment_id=tenant_cfg["tenant_id"])
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
