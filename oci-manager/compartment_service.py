"""Compartment（ compartment ）树查询与切换。

全站资源查询默认只看根 tenancy，子 compartment 里的资源完全不可见。
这里提供：

* :func:`compartment_options` —— 拉取整棵 compartment 树，带层级缩进，供下拉框使用
* :func:`apply_session_compartment` —— 把 session 里的选择绑到 tenant_cfg 上
* :func:`switch_compartment` —— 切换并做合法性校验

树结果按租户缓存 5 分钟，避免每个页面都去打 IAM API。
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from flask import flash, session

from compartment_scope import bind_compartment
from oci_helpers import get_identity_client, list_all

_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL_SECONDS = 300
#: 树遍历的保险丝：compartment 数量与 API 调用次数上限，防止异常账号把页面拖死
_MAX_NODES = 200
_MAX_CALLS = 60

ROOT_LABEL = "（根 tenancy）"


def session_key(tenant_name: str) -> str:
    return f"compartment:{tenant_name}"


def _cache_key(tenant_cfg: dict[str, Any]) -> str:
    return str(tenant_cfg.get("_tenant_name") or tenant_cfg.get("tenant_id") or "")


def _fetch_nodes(tenant_cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    client = get_identity_client(tenant_cfg)
    tenancy_id = str(tenant_cfg.get("tenant_id") or "")
    nodes: dict[str, dict[str, Any]] = {tenancy_id: {"id": tenancy_id, "name": ROOT_LABEL, "parent": None}}
    try:
        tenancy = client.get_tenancy(tenancy_id).data
        if getattr(tenancy, "name", ""):
            nodes[tenancy_id]["name"] = tenancy.name
    except Exception:
        pass

    queue = [tenancy_id]
    calls = 0
    while queue and calls < _MAX_CALLS and len(nodes) < _MAX_NODES:
        current = queue.pop(0)
        calls += 1
        try:
            children = list_all(client.list_compartments, compartment_id=current)
        except Exception:
            # 无权限或已删除的分支直接跳过，不影响其他分支
            continue
        for child in children:
            if getattr(child, "lifecycle_state", "ACTIVE") != "ACTIVE":
                continue
            if child.id in nodes:
                continue
            nodes[child.id] = {"id": child.id, "name": getattr(child, "name", "") or child.id, "parent": current}
            queue.append(child.id)
    return nodes


def _flatten(nodes: dict[str, dict[str, Any]], root_id: str) -> list[dict[str, Any]]:
    children_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes.values():
        if node["parent"]:
            children_map[node["parent"]].append(node)
    for siblings in children_map.values():
        siblings.sort(key=lambda item: (item["name"] or "").lower())

    options: list[dict[str, Any]] = []

    def walk(node_id: str, depth: int, prefix: str) -> None:
        node = nodes[node_id]
        name = node["name"]
        options.append(
            {
                "id": node_id,
                "name": name,
                "depth": depth,
                "label": ("　" * depth + ("└ " if depth else "") + name) if depth else f"{name} / {ROOT_LABEL}",
                "path": prefix + name,
            }
        )
        for child in children_map.get(node_id, []):
            walk(child["id"], depth + 1, prefix + name + " / ")

    walk(root_id, 0, "")
    return options


def compartment_options(tenant_cfg: dict[str, Any], force: bool = False) -> list[dict[str, Any]]:
    """返回当前租户可访问的 compartment 列表（含根），按层级排序。

    查询失败时静默降级为「只有根」，绝不因为拿不到树就打断页面渲染。
    """
    key = _cache_key(tenant_cfg)
    cached = _CACHE.get(key)
    if cached and not force and time.time() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    try:
        nodes = _fetch_nodes(tenant_cfg)
        options = _flatten(nodes, str(tenant_cfg.get("tenant_id") or ""))
    except Exception:
        options = [
            {
                "id": str(tenant_cfg.get("tenant_id") or ""),
                "name": ROOT_LABEL,
                "depth": 0,
                "label": ROOT_LABEL,
                "path": ROOT_LABEL,
            }
        ]
    _CACHE[key] = (time.time(), options)
    return options


def invalidate_cache(tenant_name: str = "") -> None:
    if tenant_name:
        _CACHE.pop(tenant_name, None)
    else:
        _CACHE.clear()


def find_option(options: list[dict[str, Any]], compartment_id: str) -> dict[str, Any] | None:
    return next((item for item in options if item["id"] == compartment_id), None)


def apply_session_compartment(tenant_cfg: dict[str, Any], tenant_name: str) -> dict[str, Any]:
    """把 session 中记录的 compartment 选择绑到 tenant_cfg 上。"""
    selected = session.get(session_key(tenant_name))
    if not selected:
        return tenant_cfg
    # 只信任 OCID 形态的值，避免脏 session 把非法值送进 OCI API
    if not (str(selected).startswith("ocid1.compartment.") or str(selected).startswith("ocid1.tenancy.")):
        return tenant_cfg
    return bind_compartment(tenant_cfg, str(selected), _name_of(tenant_cfg, str(selected)))


def _name_of(tenant_cfg: dict[str, Any], compartment_id: str) -> str:
    option = find_option(compartment_options(tenant_cfg), compartment_id)
    if option:
        return option["name"]
    return compartment_id


def switch_compartment(tenant_cfg: dict[str, Any], tenant_name: str, compartment_id: str) -> bool:
    """切换当前租户的 compartment。返回是否成功，失败会给出 flash 提示。"""
    compartment_id = (compartment_id or "").strip()
    tenancy_id = str(tenant_cfg.get("tenant_id") or "")
    if not compartment_id or compartment_id == tenancy_id:
        session.pop(session_key(tenant_name), None)
        return True
    options = compartment_options(tenant_cfg, force=True)
    option = find_option(options, compartment_id)
    if option is None:
        flash("该 compartment 不存在或当前密钥无权访问，已回到根 tenancy。", "error")
        session.pop(session_key(tenant_name), None)
        return False
    session[session_key(tenant_name)] = compartment_id
    flash(f"已切换到 compartment：{option['path']}", "success")
    return True


def switcher_context(tenant_cfg: dict[str, Any], tenant_name: str) -> dict[str, Any]:
    """供模板渲染 compartment 切换器使用的上下文。"""
    from compartment_scope import compartment_of

    current = compartment_of(tenant_cfg)
    options = compartment_options(tenant_cfg)
    return {
        "options": options,
        "current": current,
        "current_name": _name_of(tenant_cfg, current),
        "has_choice": len(options) > 1,
        "tenant_name": tenant_name,
    }
