"""网络：VCN / 子网 / 路由表 / 网关只读视图，以及网络安全组（NSG）规则管理。

为什么要单独做 NSG 而不是只看 Security List：OCI 里两者是**并集**生效，
只看 Security List 会让人以为端口已经关掉，实际 NSG 可能还开着——这种误判
在排查"端口怎么还能连上"时最浪费时间。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import oci

from compartment_scope import compartment_of
from oci_helpers import get_network_client, list_all
from storage import fmt_dt

_PROTOCOL_LABELS = {"1": "ICMP", "6": "TCP", "17": "UDP", "58": "ICMPv6", "all": "所有", "0": "所有"}

_DIRECTION_LABELS = {"INGRESS": "入站", "EGRESS": "出站"}

#: 协议下拉框选项。OCI 用 IANA 协议号字符串，不是协议名
NSG_PROTOCOLS = [("6", "TCP"), ("17", "UDP"), ("1", "ICMP"), ("all", "所有协议")]


def protocol_label(protocol: Any) -> str:
    key = str(protocol or "").strip().lower()
    return _PROTOCOL_LABELS.get(key, key or "-")


def direction_label(direction: Any) -> str:
    return _DIRECTION_LABELS.get(str(direction or "").upper(), str(direction or "-"))


def _safe(future, default):
    try:
        return future.result()
    except Exception:
        return default


def _port_text(rule: Any) -> str:
    protocol = str(getattr(rule, "protocol", "") or "")
    if protocol == "6":
        options = getattr(rule, "tcp_options", None)
    elif protocol == "17":
        options = getattr(rule, "udp_options", None)
    else:
        options = None
    if options is not None:
        port_range = getattr(options, "destination_port_range", None)
        if port_range is not None:
            low = getattr(port_range, "min", None)
            high = getattr(port_range, "max", None)
            if low is not None and high is not None and str(low) != str(high):
                return f"{low}-{high}"
            return str(low if low is not None else high)
        return "全部端口"
    if protocol == "1":
        options = getattr(rule, "icmp_options", None)
        if options is not None:
            return f"type {getattr(options, 'type', '-')}"
        return "全部"
    return "-"


def _cidr_text(rule: Any) -> str:
    direction = str(getattr(rule, "direction", "") or "").upper()
    if direction == "EGRESS":
        return str(getattr(rule, "destination", "") or "0.0.0.0/0")
    return str(getattr(rule, "source", "") or "0.0.0.0/0")


def _peer_type(rule: Any) -> str:
    direction = str(getattr(rule, "direction", "") or "").upper()
    value = getattr(rule, "destination_type", None) if direction == "EGRESS" else getattr(rule, "source_type", None)
    return {
        "CIDR_BLOCK": "CIDR",
        "NETWORK_SECURITY_GROUP": "NSG",
        "SERVICE_CIDR_BLOCK": "服务 CIDR",
    }.get(str(value or ""), str(value or "-"))


def network_context(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    """VCN 拓扑只读视图。所有查询并发发起，失败的部分静默降级。

    6 路并发仍要几秒，挂 TTL 缓存；写操作（POST）后由 require_tenant 失效。
    """
    from cache import tenant_cached

    return tenant_cached(tenant_cfg, "network_context", _network_context_uncached)


def _network_context_uncached(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    net = get_network_client(tenant_cfg)
    compartment = compartment_of(tenant_cfg)
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            "vcns": pool.submit(list_all, net.list_vcns, compartment_id=compartment),
            "subnets": pool.submit(list_all, net.list_subnets, compartment_id=compartment),
            "route_tables": pool.submit(list_all, net.list_route_tables, compartment_id=compartment),
            "igws": pool.submit(list_all, net.list_internet_gateways, compartment_id=compartment),
            "nats": pool.submit(list_all, net.list_nat_gateways, compartment_id=compartment),
            "sgws": pool.submit(list_all, net.list_service_gateways, compartment_id=compartment),
        }
        raw = {key: _safe(future, []) for key, future in futures.items()}

    if not raw["vcns"]:
        errors.append("当前 compartment 下没有 VCN，或密钥无权读取网络资源。")

    vcn_names = {vcn.id: (getattr(vcn, "display_name", "") or vcn.id) for vcn in raw["vcns"]}
    target_names: dict[str, str] = {}
    gateways: list[dict[str, Any]] = []
    for key, kind, state_field in (
        ("igws", "互联网网关", None),
        ("nats", "NAT 网关", None),
        ("sgws", "服务网关", None),
    ):
        for item in raw[key]:
            target_names[item.id] = getattr(item, "display_name", "") or item.id
            gateways.append(
                {
                    "id": item.id,
                    "name": getattr(item, "display_name", "") or "-",
                    "kind": kind,
                    "vcn": vcn_names.get(getattr(item, "vcn_id", ""), "-"),
                    "state": getattr(item, "lifecycle_state", "-"),
                }
            )
    for drg_route in raw["route_tables"]:
        for rule in getattr(drg_route, "route_rules", None) or []:
            entity = getattr(rule, "network_entity_id", "")
            if entity and entity not in target_names:
                target_names[entity] = entity[:20] + "…"

    route_tables = []
    for table in raw["route_tables"]:
        rules = []
        for rule in getattr(table, "route_rules", None) or []:
            entity = getattr(rule, "network_entity_id", "")
            rules.append(
                {
                    "destination": getattr(rule, "destination", "-"),
                    "destination_type": getattr(rule, "destination_type", "-"),
                    "target": target_names.get(entity, entity or "-"),
                }
            )
        route_tables.append(
            {
                "id": table.id,
                "name": getattr(table, "display_name", "") or "-",
                "vcn": vcn_names.get(getattr(table, "vcn_id", ""), "-"),
                "rules": rules,
            }
        )

    # 子网是否公有：优先看 prohibit_public_ip_on_vnic，其次看路由表是否指向公网网关
    public_routes = {item.id for item in raw["igws"]} | {item.id for item in raw["nats"]}
    table_has_public = {}
    for table in raw["route_tables"]:
        table_has_public[table.id] = any(
            (getattr(rule, "network_entity_id", "") in public_routes)
            for rule in (getattr(table, "route_rules", None) or [])
        )

    subnets = []
    for subnet in raw["subnets"]:
        prohibit = getattr(subnet, "prohibit_public_ip_on_vnic", None)
        if prohibit is not None:
            is_public = not bool(prohibit)
            public_reason = "允许公网 IP" if is_public else "禁止公网 IP"
        else:
            is_public = table_has_public.get(getattr(subnet, "route_table_id", ""), False)
            public_reason = "路由表含公网网关" if is_public else "路由表无公网网关"
        subnets.append(
            {
                "id": subnet.id,
                "name": getattr(subnet, "display_name", "") or "-",
                "cidr": getattr(subnet, "cidr_block", "-"),
                "vcn": vcn_names.get(getattr(subnet, "vcn_id", ""), "-"),
                "availability_domain": getattr(subnet, "availability_domain", "") or "区域级",
                "public": is_public,
                "public_reason": public_reason,
                "dns_label": getattr(subnet, "dns_label", "") or "-",
                "security_list_count": len(getattr(subnet, "security_list_ids", None) or []),
                "created": fmt_dt(getattr(subnet, "time_created", "")),
            }
        )

    vcns = [
        {
            "id": vcn.id,
            "name": getattr(vcn, "display_name", "") or "-",
            "cidr": getattr(vcn, "cidr_block", "-"),
            "state": getattr(vcn, "lifecycle_state", "-"),
            "dns_label": getattr(vcn, "dns_label", "") or "-",
            "subnet_count": sum(1 for item in raw["subnets"] if getattr(item, "vcn_id", "") == vcn.id),
            "created": fmt_dt(getattr(vcn, "time_created", "")),
        }
        for vcn in raw["vcns"]
    ]

    return {
        "vcns": vcns,
        "subnets": subnets,
        "route_tables": route_tables,
        "gateways": gateways,
        "load_error": " ".join(errors),
    }


def nsg_context(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    """列出 NSG，并附带每个 NSG 关联的网卡数量。"""
    net = get_network_client(tenant_cfg)
    compartment = compartment_of(tenant_cfg)
    nsgs = list_all(net.list_network_security_groups, compartment_id=compartment)

    def vnic_count(nsg_id: str) -> int:
        try:
            return len(list_all(net.list_network_security_group_vnics, nsg_id))
        except Exception:
            return -1

    rows: list[dict[str, Any]] = []
    if nsgs:
        with ThreadPoolExecutor(max_workers=min(8, len(nsgs))) as pool:
            counts = list(pool.map(lambda item: vnic_count(item.id), nsgs))
    else:
        counts = []
    for nsg, count in zip(nsgs, counts):
        rows.append(
            {
                "id": nsg.id,
                "name": getattr(nsg, "display_name", "") or "-",
                "vcn_id": getattr(nsg, "vcn_id", ""),
                "state": getattr(nsg, "lifecycle_state", "-"),
                "vnic_count": count,
                "created": fmt_dt(getattr(nsg, "time_created", "")),
            }
        )
    return {"nsgs": rows}


def nsg_rules_context(tenant_cfg: dict[str, Any], nsg_id: str) -> dict[str, Any]:
    net = get_network_client(tenant_cfg)
    nsg = net.get_network_security_group(nsg_id).data
    rules = []
    for item in list_all(net.list_network_security_group_security_rules, nsg_id):
        rules.append(
            {
                "id": item.id,
                "direction": getattr(item, "direction", "-"),
                "direction_label": direction_label(getattr(item, "direction", "")),
                "protocol": protocol_label(getattr(item, "protocol", "")),
                "raw_protocol": str(getattr(item, "protocol", "") or ""),
                "cidr": _cidr_text(item),
                "peer_type": _peer_type(item),
                "ports": _port_text(item),
                "description": getattr(item, "description", "") or "",
                "is_valid": getattr(item, "is_valid", True),
                "stateless": bool(getattr(item, "is_stateless", False)),
                "created": fmt_dt(getattr(item, "time_created", "")),
            }
        )
    rules.sort(key=lambda row: (row["direction"] != "INGRESS", row["created"]))
    return {
        "nsg": {
            "id": nsg.id,
            "name": getattr(nsg, "display_name", "") or "-",
            "state": getattr(nsg, "lifecycle_state", "-"),
        },
        "rules": rules,
    }


def _build_rule_details(direction: str, protocol: str, cidr: str, ports: str, description: str, stateless: bool) -> dict[str, Any]:
    protocol = (protocol or "").strip() or "all"
    cidr = (cidr or "").strip() or ("0.0.0.0/0" if direction == "INGRESS" else "0.0.0.0/0")
    payload: dict[str, Any] = {
        "direction": direction,
        "protocol": protocol,
        "description": (description or "").strip(),
        "is_stateless": bool(stateless),
    }
    if direction == "INGRESS":
        payload["source"] = cidr
        payload["source_type"] = "CIDR_BLOCK"
    else:
        payload["destination"] = cidr
        payload["destination_type"] = "CIDR_BLOCK"

    if protocol == "6" and ports and ports.strip() not in {"", "-"}:
        low, _, high = ports.partition("-")
        payload["tcp_options"] = oci.core.models.TcpOptions(
            destination_port_range=oci.core.models.PortRange(min=int(low), max=int(high or low))
        )
    elif protocol == "17" and ports and ports.strip() not in {"", "-"}:
        low, _, high = ports.partition("-")
        payload["udp_options"] = oci.core.models.UdpOptions(
            destination_port_range=oci.core.models.PortRange(min=int(low), max=int(high or low))
        )
    return payload


def add_nsg_rule(
    tenant_cfg: dict[str, Any],
    nsg_id: str,
    direction: str,
    protocol: str,
    cidr: str,
    ports: str,
    description: str,
    stateless: bool = False,
) -> None:
    net = get_network_client(tenant_cfg)
    payload = _build_rule_details(direction, protocol, cidr, ports, description, stateless)
    net.add_network_security_group_security_rules(
        nsg_id,
        oci.core.models.AddNetworkSecurityGroupSecurityRulesDetails(
            security_rules=[oci.core.models.AddSecurityRuleDetails(**payload)]
        ),
    )


def remove_nsg_rule(tenant_cfg: dict[str, Any], nsg_id: str, rule_id: str) -> None:
    net = get_network_client(tenant_cfg)
    net.remove_network_security_group_security_rules(
        nsg_id,
        oci.core.models.RemoveNetworkSecurityGroupSecurityRulesDetails(security_rule_ids=[rule_id]),
    )


def update_nsg_rule(
    tenant_cfg: dict[str, Any],
    nsg_id: str,
    rule_id: str,
    direction: str,
    protocol: str,
    cidr: str,
    ports: str,
    description: str,
    stateless: bool = False,
) -> None:
    net = get_network_client(tenant_cfg)
    payload = _build_rule_details(direction, protocol, cidr, ports, description, stateless)
    payload["id"] = rule_id
    net.update_network_security_group_security_rules(
        nsg_id,
        oci.core.models.UpdateNetworkSecurityGroupSecurityRulesDetails(
            security_rules=[oci.core.models.UpdateSecurityRuleDetails(**payload)]
        ),
    )
