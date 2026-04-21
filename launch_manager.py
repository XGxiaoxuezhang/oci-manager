from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any

import oci

from oci_helpers import get_compute_client, get_identity_client, get_network_client, list_all
from settings import LAUNCH_PRESETS, RETRYABLE_KEYWORDS
from storage import now_iso

LAUNCH_TASKS: dict[str, dict[str, Any]] = {}
TASK_LOCK = threading.Lock()


def is_retryable_launch_error(exc: Exception) -> bool:
    text = str(exc).lower()
    status = getattr(exc, "status", None)
    return status in {429, 500, 502, 503, 504} or any(keyword in text for keyword in RETRYABLE_KEYWORDS)


def task_snapshot(task_id: str) -> dict[str, Any] | None:
    with TASK_LOCK:
        task = LAUNCH_TASKS.get(task_id)
        return None if task is None else {**task, "logs": list(task.get("logs", []))}


def update_task(task_id: str, **kwargs: Any) -> None:
    with TASK_LOCK:
        task = LAUNCH_TASKS.get(task_id)
        if task is not None:
            task.update(kwargs)


def append_task_log(task_id: str, message: str) -> None:
    with TASK_LOCK:
        task = LAUNCH_TASKS.get(task_id)
        if task is None:
            return
        task.setdefault("logs", []).append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        task["updated_at"] = now_iso()


def filtered_tasks(tenant_name: str) -> list[dict[str, Any]]:
    with TASK_LOCK:
        tasks = [{**task, "logs": list(task.get("logs", []))} for task in LAUNCH_TASKS.values() if task.get("tenant_name") == tenant_name]
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return tasks[:12]


def discover_candidate_images(compute_client: oci.core.ComputeClient, compartment_id: str) -> list[dict[str, str]]:
    candidates = [("Canonical Ubuntu", "24.04"), ("Canonical Ubuntu", "22.04"), ("Oracle Linux", "9"), ("Oracle Linux", "8"), ("Debian", "12")]
    results: list[dict[str, str]] = []
    for os_name, version in candidates:
        try:
            images = list_all(
                compute_client.list_images,
                compartment_id=compartment_id,
                operating_system=os_name,
                operating_system_version=version,
                sort_by="TIMECREATED",
                sort_order="DESC",
            )
        except Exception:
            continue
        image = next((img for img in images if getattr(img, "lifecycle_state", "") == "AVAILABLE"), None)
        if image:
            results.append({"id": image.id, "label": f"{os_name} {version}", "display_name": image.display_name})
    return results


def launch_context(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    identity_client = get_identity_client(tenant_cfg)
    network_client = get_network_client(tenant_cfg)
    compute_client = get_compute_client(tenant_cfg)
    availability_domains = [ad.name for ad in list_all(identity_client.list_availability_domains, compartment_id=tenant_cfg["tenant_id"])]
    subnets = [
        {
            "id": subnet.id,
            "name": subnet.display_name,
            "cidr": subnet.cidr_block,
            "availability_domain": subnet.availability_domain or "Regional",
        }
        for subnet in list_all(network_client.list_subnets, compartment_id=tenant_cfg["tenant_id"])
    ]
    subnets.sort(key=lambda item: (item["name"] or "", item["availability_domain"]))
    return {
        "availability_domains": availability_domains,
        "subnets": subnets,
        "images": discover_candidate_images(compute_client, tenant_cfg["tenant_id"]),
    }


def build_launch_details(tenant_cfg: dict[str, Any], form: dict[str, Any]) -> oci.core.models.LaunchInstanceDetails:
    preset_key = form.get("preset") or "amd1c1g"
    preset = LAUNCH_PRESETS.get(preset_key, LAUNCH_PRESETS["amd1c1g"])
    shape = (form.get("custom_shape") or "").strip() or preset["shape"]
    ocpus_raw = (form.get("ocpus") or "").strip()
    memory_raw = (form.get("memory_gbs") or "").strip()
    boot_volume_raw = (form.get("boot_volume_gbs") or "").strip()
    display_name = (form.get("display_name") or "").strip() or f"{preset_key}-{datetime.now().strftime('%m%d-%H%M%S')}"
    image_id = (form.get("image_id") or "").strip()
    if not image_id:
        raise ValueError("请选择或填写镜像 OCID。")

    source_details = oci.core.models.InstanceSourceViaImageDetails(source_type="image", image_id=image_id)
    if boot_volume_raw:
        source_details.boot_volume_size_in_gbs = int(boot_volume_raw)
    elif preset.get("boot_volume_gbs"):
        source_details.boot_volume_size_in_gbs = int(preset["boot_volume_gbs"])

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=form["availability_domain"],
        compartment_id=tenant_cfg["tenant_id"],
        display_name=display_name,
        shape=shape,
        source_details=source_details,
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=form["subnet_id"],
            assign_public_ip=form.get("assign_public_ip") == "on",
            display_name=f"{display_name}-vnic",
        ),
        metadata={"ssh_authorized_keys": (form.get("ssh_public_key") or "").strip()} if (form.get("ssh_public_key") or "").strip() else {},
    )

    ocpus = float(ocpus_raw) if ocpus_raw else preset.get("ocpus")
    memory_gbs = float(memory_raw) if memory_raw else preset.get("memory_gbs")
    if ocpus is not None or memory_gbs is not None:
        details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=memory_gbs)
    return details


def launch_worker(task_id: str, tenant_cfg: dict[str, Any], form: dict[str, Any]) -> None:
    compute_client = get_compute_client(tenant_cfg)
    attempts = int(form.get("attempts") or 60)
    interval_seconds = int(form.get("interval_seconds") or 20)
    update_task(task_id, status="running")
    append_task_log(task_id, f"任务开始，最多尝试 {attempts} 次，每次间隔 {interval_seconds} 秒。")
    for attempt in range(1, attempts + 1):
        snapshot = task_snapshot(task_id)
        if not snapshot or snapshot.get("cancel_requested"):
            update_task(task_id, status="cancelled", finished_at=now_iso())
            append_task_log(task_id, "任务已取消。")
            return
        update_task(task_id, current_attempt=attempt)
        append_task_log(task_id, f"第 {attempt} 次尝试发起创建实例请求。")
        try:
            instance = compute_client.launch_instance(build_launch_details(tenant_cfg, form)).data
            update_task(task_id, status="success", instance_id=instance.id, instance_name=instance.display_name, finished_at=now_iso())
            append_task_log(task_id, f"抢机成功：{instance.display_name} / {instance.id}")
            return
        except Exception as exc:
            append_task_log(task_id, f"尝试失败：{exc}")
            if attempt >= attempts or not is_retryable_launch_error(exc):
                update_task(task_id, status="failed", last_error=str(exc), finished_at=now_iso())
                append_task_log(task_id, "任务结束，未再继续重试。")
                return
            time.sleep(interval_seconds)
    update_task(task_id, status="failed", finished_at=now_iso())
    append_task_log(task_id, "任务结束，达到最大重试次数。")
