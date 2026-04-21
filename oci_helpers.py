from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any

import oci

from settings import OCI_CONNECT_TIMEOUT, OCI_READ_TIMEOUT, TENANT_DIR, format_region
from storage import fmt_dt, load_tenants, save_tenants


def clear_broken_proxy_env() -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)


clear_broken_proxy_env()


def _resolve_key_path(tenant_cfg: dict[str, Any]) -> str:
    key_path = tenant_cfg.get("key_path", "")
    if key_path and Path(key_path).exists():
        return key_path
    tenant_name = tenant_cfg.get("_tenant_name")
    if tenant_name:
        fallback = TENANT_DIR / tenant_name / "key.pem"
        if fallback.exists():
            return str(fallback)
    return key_path


def build_config(tenant_cfg: dict[str, Any]) -> dict[str, str]:
    config = {
        "user": tenant_cfg["user_id"],
        "tenancy": tenant_cfg["tenant_id"],
        "region": tenant_cfg["region"],
        "key_file": _resolve_key_path(tenant_cfg),
        "fingerprint": tenant_cfg["fingerprint"],
    }
    oci.config.validate_config(config)
    return config


def client_kwargs() -> dict[str, Any]:
    return {"timeout": (OCI_CONNECT_TIMEOUT, OCI_READ_TIMEOUT)}


def list_all(func: Any, *args: Any, **kwargs: Any) -> list[Any]:
    return oci.pagination.list_call_get_all_results(func, *args, **kwargs).data


def find_tenant_config(tenant_name: str) -> dict[str, Any] | None:
    tenants = load_tenants()
    tenant_cfg = tenants.get(tenant_name)
    if tenant_cfg is None:
        return None
    tenant_cfg = dict(tenant_cfg)
    tenant_cfg["_tenant_name"] = tenant_name
    resolved_key_path = _resolve_key_path(tenant_cfg)
    if resolved_key_path and resolved_key_path != tenant_cfg.get("key_path"):
        tenants[tenant_name]["key_path"] = resolved_key_path
        save_tenants(tenants)
        tenant_cfg["key_path"] = resolved_key_path
    return tenant_cfg


def get_identity_client(tenant_cfg: dict[str, Any]) -> oci.identity.IdentityClient:
    return oci.identity.IdentityClient(build_config(tenant_cfg), **client_kwargs())


def get_compute_client(tenant_cfg: dict[str, Any]) -> oci.core.ComputeClient:
    return oci.core.ComputeClient(build_config(tenant_cfg), **client_kwargs())


def get_network_client(tenant_cfg: dict[str, Any]) -> oci.core.VirtualNetworkClient:
    return oci.core.VirtualNetworkClient(build_config(tenant_cfg), **client_kwargs())


def get_block_client(tenant_cfg: dict[str, Any]) -> oci.core.BlockstorageClient:
    return oci.core.BlockstorageClient(build_config(tenant_cfg), **client_kwargs())


def validate_cidr(value: str) -> None:
    ipaddress.ip_network(value, strict=False)


def create_port_options(protocol: str, port_min: str, port_max: str) -> Any | None:
    if protocol not in {"tcp", "udp"}:
        return None
    if not port_min and not port_max:
        return None
    if not (port_min and port_max):
        raise ValueError("端口范围需要同时填写最小值和最大值。")
    start = int(port_min)
    end = int(port_max)
    if start < 1 or end > 65535 or start > end:
        raise ValueError("端口范围必须在 1-65535 之间，且起始值不能大于结束值。")
    port_range = oci.core.models.PortRange(min=start, max=end)
    if protocol == "tcp":
        return oci.core.models.TcpOptions(destination_port_range=port_range)
    return oci.core.models.UdpOptions(destination_port_range=port_range)


def build_security_rule(
    rule_type: str,
    protocol: str,
    cidr: str,
    port_min: str,
    port_max: str,
    description: str,
) -> Any:
    validate_cidr(cidr)
    normalized_protocol = "all" if protocol == "all" else protocol
    base_args = {"protocol": normalized_protocol, "description": description or None}
    options = create_port_options(protocol, port_min, port_max)
    if rule_type == "ingress":
        if options and protocol == "tcp":
            return oci.core.models.IngressSecurityRule(source=cidr, tcp_options=options, **base_args)
        if options and protocol == "udp":
            return oci.core.models.IngressSecurityRule(source=cidr, udp_options=options, **base_args)
        return oci.core.models.IngressSecurityRule(source=cidr, **base_args)
    if options and protocol == "tcp":
        return oci.core.models.EgressSecurityRule(destination=cidr, tcp_options=options, **base_args)
    if options and protocol == "udp":
        return oci.core.models.EgressSecurityRule(destination=cidr, udp_options=options, **base_args)
    return oci.core.models.EgressSecurityRule(destination=cidr, **base_args)


def extract_rule_ports(rule: Any) -> str:
    tcp_options = getattr(rule, "tcp_options", None)
    udp_options = getattr(rule, "udp_options", None)
    options = tcp_options or udp_options
    port_range = getattr(options, "destination_port_range", None)
    if port_range and port_range.min is not None and port_range.max is not None:
        return f"{port_range.min}-{port_range.max}"
    return "全部"


def summarize_rule(direction: str, index: int, rule: Any) -> dict[str, Any]:
    peer_key = "source" if direction == "ingress" else "destination"
    return {
        "index": index,
        "protocol": getattr(rule, "protocol", "all"),
        "peer": getattr(rule, peer_key, "0.0.0.0/0"),
        "ports": extract_rule_ports(rule),
        "description": getattr(rule, "description", "") or "",
    }


def build_dashboard_cards(tenants: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": tenant_name,
            "region": tenant_cfg.get("region", "-"),
            "region_label": format_region(tenant_cfg.get("region", "")),
            "tenant_id": tenant_cfg.get("tenant_id", ""),
            "created": fmt_dt(tenant_cfg.get("created")),
            "key_path": tenant_cfg.get("key_path", ""),
        }
        for tenant_name, tenant_cfg in tenants.items()
    ]


def primary_vnic(
    network_client: oci.core.VirtualNetworkClient,
    compute_client: oci.core.ComputeClient,
    compartment_id: str,
    instance_id: str,
) -> Any | None:
    attachments = list_all(compute_client.list_vnic_attachments, compartment_id=compartment_id, instance_id=instance_id)
    primary = next((item for item in attachments if getattr(item, "is_primary", False)), attachments[0] if attachments else None)
    if primary and primary.vnic_id:
        return network_client.get_vnic(primary.vnic_id).data
    return None


def primary_private_ip(network_client: oci.core.VirtualNetworkClient, vnic_id: str) -> Any | None:
    private_ips = list_all(network_client.list_private_ips, vnic_id=vnic_id)
    return next((item for item in private_ips if getattr(item, "is_primary", False)), private_ips[0] if private_ips else None)


def instance_boot_volume(tenant_cfg: dict[str, Any], instance: Any) -> tuple[Any | None, Any | None]:
    compute_client = get_compute_client(tenant_cfg)
    block_client = get_block_client(tenant_cfg)
    attachments = list_all(
        compute_client.list_boot_volume_attachments,
        instance.availability_domain,
        tenant_cfg["tenant_id"],
        instance_id=instance.id,
    )
    if not attachments:
        return None, None
    attachment = attachments[0]
    boot_volume = block_client.get_boot_volume(attachment.boot_volume_id).data
    return attachment, boot_volume


def instance_console_connections(tenant_cfg: dict[str, Any], instance_id: str) -> list[dict[str, Any]]:
    compute_client = get_compute_client(tenant_cfg)
    connections = list_all(
        compute_client.list_instance_console_connections,
        compartment_id=tenant_cfg["tenant_id"],
        instance_id=instance_id,
    )
    rows = []
    for item in connections:
        detail = item
        try:
            detail = compute_client.get_instance_console_connection(item.id).data
        except Exception:
            detail = item
        rows.append(
            {
                "id": item.id,
                "state": getattr(detail, "lifecycle_state", "-"),
                "created": fmt_dt(getattr(detail, "time_created", None)),
                "fingerprint": getattr(detail, "fingerprint", "-"),
                "service_host_key_fingerprint": getattr(detail, "service_host_key_fingerprint", "-"),
                "connection_string": getattr(detail, "connection_string", "") or "",
                "vnc_connection_string": getattr(detail, "vnc_connection_string", "") or "",
            }
        )
    rows.sort(key=lambda row: row["created"], reverse=True)
    return rows
