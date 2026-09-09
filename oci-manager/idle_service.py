from __future__ import annotations

from typing import Any

from object_storage_service import storage_context
from oci_helpers import get_block_client, get_compute_client, get_identity_client, list_all
from storage import fmt_dt
from compartment_scope import compartment_of


def _idle_instances(tenant_cfg: dict[str, Any]) -> list[dict[str, str]]:
    compute = get_compute_client(tenant_cfg)
    identity = get_identity_client(tenant_cfg)
    rows: list[dict[str, str]] = []
    for ad in list_all(identity.list_availability_domains, compartment_id=tenant_cfg["tenant_id"]):
        for instance in list_all(compute.list_instances, compartment_id=compartment_of(tenant_cfg), availability_domain=ad.name):
            state = getattr(instance, "lifecycle_state", "")
            if state == "STOPPED":
                rows.append(
                    {
                        "kind": "instance",
                        "name": getattr(instance, "display_name", instance.id),
                        "detail": f"{getattr(instance, 'shape', '-')} · {state}",
                        "created": fmt_dt(getattr(instance, "time_created", None)),
                    }
                )
    return rows


def _unattached_boot_volumes(tenant_cfg: dict[str, Any]) -> list[dict[str, str]]:
    block = get_block_client(tenant_cfg)
    identity = get_identity_client(tenant_cfg)
    rows: list[dict[str, str]] = []
    for ad in list_all(identity.list_availability_domains, compartment_id=tenant_cfg["tenant_id"]):
        for vol in list_all(block.list_boot_volumes, compartment_id=compartment_of(tenant_cfg), availability_domain=ad.name):
            state = getattr(vol, "lifecycle_state", "")
            if state not in {"TERMINATED", "TERMINATING"} and not getattr(vol, "instance_id", None):
                rows.append(
                    {
                        "kind": "boot_volume",
                        "name": getattr(vol, "display_name", vol.id),
                        "detail": f"{getattr(vol, 'size_in_gbs', '-')} GB · 未挂载实例",
                        "created": fmt_dt(getattr(vol, "time_created", None)),
                    }
                )
    return rows


def _empty_buckets(tenant_cfg: dict[str, Any]) -> list[dict[str, str]]:
    ctx = storage_context(tenant_cfg)
    rows: list[dict[str, str]] = []
    for bucket in ctx.get("buckets", []):
        if int(bucket.get("count", 0) or 0) == 0:
            rows.append(
                {
                    "kind": "bucket",
                    "name": bucket["name"],
                    "detail": f"对象数 0 · {bucket.get('storage_tier', '-')}",
                    "created": bucket.get("created", "-"),
                }
            )
    return rows


def idle_resources_context(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {"instances": [], "boot_volumes": [], "buckets": []}
    try:
        results["instances"] = _idle_instances(tenant_cfg)
    except Exception as exc:
        results["instances_error"] = str(exc)
    try:
        results["boot_volumes"] = _unattached_boot_volumes(tenant_cfg)
    except Exception as exc:
        results["boot_volumes_error"] = str(exc)
    try:
        results["buckets"] = _empty_buckets(tenant_cfg)
    except Exception as exc:
        results["buckets_error"] = str(exc)
    results["total"] = len(results["instances"]) + len(results["boot_volumes"]) + len(results["buckets"])
    return results
