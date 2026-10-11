"""当前生效 compartment 的取值约定。

这个模块刻意不 import 任何东西（包括 flask 和 oci），因为 oci_helpers 与
compartment_service 都要用它，放这里才能避免循环导入。

约定：路由层在拿到 tenant_cfg 后调用 ``bind_compartment()`` 把当前选择的
compartment 写进 ``_compartment_id``；服务层一律通过 ``compartment_of()``
读取，读不到就回落到根 tenancy。这样后台线程（抢实例任务）也能拿到正确值，
因为它拿的是同一个已经绑定过的 dict，不依赖 request context。
"""

from __future__ import annotations

from typing import Any

#: 绑定到 tenant_cfg 上的 compartment OCID 键名
COMPARTMENT_KEY = "_compartment_id"
#: 绑定到 tenant_cfg 上的 compartment 显示名键名
COMPARTMENT_NAME_KEY = "_compartment_name"


def compartment_of(tenant_cfg: dict[str, Any]) -> str:
    """当前生效的 compartment OCID。未绑定时回落根 tenancy。"""
    return str(tenant_cfg.get(COMPARTMENT_KEY) or tenant_cfg.get("tenant_id") or "")


def compartment_name_of(tenant_cfg: dict[str, Any]) -> str:
    """当前生效 compartment 的显示名，供 UI 展示。"""
    return str(tenant_cfg.get(COMPARTMENT_NAME_KEY) or "")


def bind_compartment(tenant_cfg: dict[str, Any], compartment_id: str, compartment_name: str = "") -> dict[str, Any]:
    """返回绑定了 compartment 的 tenant_cfg 副本，不改动原对象。"""
    cfg = dict(tenant_cfg)
    cfg[COMPARTMENT_KEY] = compartment_id or cfg.get("tenant_id") or ""
    cfg[COMPARTMENT_NAME_KEY] = compartment_name
    return cfg


def is_root_compartment(tenant_cfg: dict[str, Any]) -> bool:
    return compartment_of(tenant_cfg) == str(tenant_cfg.get("tenant_id") or "")
