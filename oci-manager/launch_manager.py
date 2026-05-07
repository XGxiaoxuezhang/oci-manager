from __future__ import annotations

import threading
import time
import json
from datetime import datetime
from typing import Any

import oci

from db import connect
from notify import notify
from oci_helpers import get_compute_client, get_identity_client, get_network_client, list_all
from settings import LAUNCH_PRESETS, LAUNCH_TASKS_PATH, RETRYABLE_KEYWORDS
from storage import now_iso

LAUNCH_TASKS: dict[str, dict[str, Any]] = {}
TASK_LOCK = threading.RLock()


def load_launch_tasks() -> None:
    loaded = load_launch_tasks_from_db()
    if loaded:
        return
    try:
        raw = json.loads(LAUNCH_TASKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    with TASK_LOCK:
        LAUNCH_TASKS.clear()
        changed = False
        for task_id, task in raw.items():
            if not isinstance(task, dict):
                continue
            task = dict(task)
            if task.get("status") in {"queued", "running"}:
                task["status"] = "interrupted"
                task["finished_at"] = task.get("finished_at") or now_iso()
                task.setdefault("logs", []).append(f"[{datetime.now().strftime('%H:%M:%S')}] 服务重启，任务已标记为中断。")
                changed = True
            LAUNCH_TASKS[str(task_id)] = task
    if changed:
        save_launch_tasks()


def load_launch_tasks_from_db() -> bool:
    try:
        with connect() as conn:
            rows = conn.execute("SELECT task_json FROM launch_tasks ORDER BY created_at DESC").fetchall()
    except Exception:
        return False
    if not rows:
        return False
    changed = False
    with TASK_LOCK:
        LAUNCH_TASKS.clear()
        for row in rows:
            try:
                task = json.loads(row["task_json"])
            except json.JSONDecodeError:
                continue
            task_id = str(task.get("id") or "")
            if not task_id:
                continue
            if task.get("status") in {"queued", "running"}:
                task["status"] = "interrupted"
                task["finished_at"] = task.get("finished_at") or now_iso()
                task.setdefault("logs", []).append(f"[{datetime.now().strftime('%H:%M:%S')}] 服务重启，任务已标记为中断。")
                changed = True
            LAUNCH_TASKS[task_id] = task
    if changed:
        save_launch_tasks()
    return True


def save_launch_tasks() -> None:
    with TASK_LOCK:
        payload = {task_id: task for task_id, task in LAUNCH_TASKS.items()}
    try:
        with connect() as conn:
            for task_id, task in payload.items():
                conn.execute(
                    """
                    INSERT INTO launch_tasks (id, tenant_name, status, created_at, updated_at, task_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        tenant_name=excluded.tenant_name,
                        status=excluded.status,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at,
                        task_json=excluded.task_json
                    """,
                    (
                        task_id,
                        task.get("tenant_name", "-"),
                        task.get("status", "-"),
                        task.get("created_at", ""),
                        task.get("updated_at", ""),
                        json.dumps(task, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
    except Exception:
        try:
            LAUNCH_TASKS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return


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
            task["updated_at"] = now_iso()
    save_launch_tasks()


def clear_finished_tasks(tenant_name: str) -> int:
    finished = {"success", "failed", "cancelled", "interrupted"}
    removed = 0
    with TASK_LOCK:
        for task_id, task in list(LAUNCH_TASKS.items()):
            if task.get("tenant_name") == tenant_name and task.get("status") in finished:
                LAUNCH_TASKS.pop(task_id, None)
                removed += 1
    if removed:
        save_launch_tasks()
        try:
            with connect() as conn:
                conn.execute(
                    "DELETE FROM launch_tasks WHERE tenant_name = ? AND status IN ('success', 'failed', 'cancelled', 'interrupted')",
                    (tenant_name,),
                )
        except Exception:
            pass
    return removed


def append_task_log(task_id: str, message: str) -> None:
    with TASK_LOCK:
        task = LAUNCH_TASKS.get(task_id)
        if task is None:
            return
        task.setdefault("logs", []).append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        task["updated_at"] = now_iso()
    save_launch_tasks()


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

    try:
        ocpus = float(ocpus_raw) if ocpus_raw else preset.get("ocpus")
        memory_gbs = float(memory_raw) if memory_raw else preset.get("memory_gbs")
    except ValueError as exc:
        raise ValueError("OCPU 或内存格式不正确。") from exc
    if ocpus is not None or memory_gbs is not None:
        details.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=memory_gbs)
    return details


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def launch_worker(task_id: str, tenant_cfg: dict[str, Any], form: dict[str, Any]) -> None:
    compute_client = get_compute_client(tenant_cfg)
    attempts = bounded_int(form.get("attempts"), 60, 1, 9999)
    interval_seconds = bounded_int(form.get("interval_seconds"), 20, 5, 300)
    update_task(task_id, status="running")
    append_task_log(task_id, f"任务开始，最多尝试 {attempts} 次，每次间隔 {interval_seconds} 秒。")
    for attempt in range(1, attempts + 1):
        snapshot = task_snapshot(task_id)
        if not snapshot or snapshot.get("cancel_requested"):
            update_task(task_id, status="cancelled", finished_at=now_iso())
            append_task_log(task_id, "任务已取消。")
            notify("OCI 创建任务已取消", f"{task_id} 已取消。", {"tenant_name": tenant_cfg.get("_tenant_name")})
            return
        update_task(task_id, current_attempt=attempt)
        append_task_log(task_id, f"第 {attempt} 次尝试发起创建实例请求。")
        try:
            instance = compute_client.launch_instance(build_launch_details(tenant_cfg, form)).data
            update_task(task_id, status="success", instance_id=instance.id, instance_name=instance.display_name, finished_at=now_iso())
            append_task_log(task_id, f"创建成功：{instance.display_name} / {instance.id}")
            notify("OCI 创建实例成功", f"{task_id}: {instance.display_name}", {"tenant_name": tenant_cfg.get("_tenant_name"), "instance_id": instance.id})
            return
        except Exception as exc:
            append_task_log(task_id, f"尝试失败：{exc}")
            if attempt >= attempts or not is_retryable_launch_error(exc):
                update_task(task_id, status="failed", last_error=str(exc), finished_at=now_iso())
                append_task_log(task_id, "任务结束，未再继续重试。")
                notify("OCI 创建实例失败", f"{task_id}: {exc}", {"tenant_name": tenant_cfg.get("_tenant_name")})
                return
            time.sleep(interval_seconds)
    update_task(task_id, status="failed", finished_at=now_iso())
    append_task_log(task_id, "任务结束，达到最大重试次数。")
    notify("OCI 创建实例失败", f"{task_id}: 达到最大重试次数。", {"tenant_name": tenant_cfg.get("_tenant_name")})


load_launch_tasks()
