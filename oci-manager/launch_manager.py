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
from settings import LAUNCH_PRESETS, RETRYABLE_KEYWORDS
from storage import now_iso
from compartment_scope import compartment_of

LAUNCH_TASKS: dict[str, dict[str, Any]] = {}
TASK_LOCK = threading.RLock()


def load_launch_tasks() -> None:
    """服务启动时恢复任务表，并把上次运行遗留的 queued/running 标记为 interrupted。

    由 app 显式调用，不在 import 时执行——import 触发磁盘 IO 和状态改写会让
    测试与工具脚本难以复用这个模块。
    """
    load_launch_tasks_from_db()
    _import_legacy_json_tasks()


def _import_legacy_json_tasks() -> None:
    """旧版本任务存 launch_tasks.json；一次性导入 SQLite 后改名归档。"""
    from settings import DATA_DIR

    legacy_path = DATA_DIR / "launch_tasks.json"
    if not legacy_path.exists():
        return
    try:
        data = json.loads(legacy_path.read_text(encoding="utf-8"))
        tasks = data.get("tasks", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        rows = []
        for task in tasks:
            task_id = str(task.get("id") or "")
            if not task_id or task_id in LAUNCH_TASKS:
                continue
            if task.get("status") in {"queued", "running"}:
                task["status"] = "interrupted"
            task.setdefault("logs", [])
            rows.append(task)
        if rows:
            with TASK_LOCK:
                with connect() as conn:
                    _upsert_tasks(conn, {task["id"]: task for task in rows})
                for task in rows:
                    LAUNCH_TASKS[task["id"]] = task
        legacy_path.rename(legacy_path.with_name("launch_tasks.json.imported"))
        print(f"[launch_manager] 已导入 {len(rows)} 条历史任务（launch_tasks.json → SQLite）", flush=True)
    except Exception as exc:
        print(f"[launch_manager] 导入 legacy launch_tasks.json 失败: {exc}", flush=True)


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


def _upsert_tasks(conn, payload: dict[str, dict[str, Any]]) -> None:
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


def save_launch_tasks() -> None:
    """把任务表整体写入 SQLite。

    早期版本在写库失败时回退写 JSON，导致同一份数据有两个可能的来源、
    读的时候又要按优先级猜。现在 JSON 回退已移除：写库失败就是失败，
    宁可在页面上暴露，也不要留下两份会互相覆盖的真值。

    整个流程（取快照 + 写库）都持 TASK_LOCK：TASK_LOCK 是 RLock，update_task /
    append_task_log 持锁调用这里也不会死锁；锁内写库保证两个线程的落盘顺序
    与内存修改顺序一致，否则快照乱序会让重启后恢复出旧状态。

    高频路径（状态变更/追加日志）请用 save_launch_task，只写单行。
    """
    with TASK_LOCK:
        payload = {task_id: task for task_id, task in LAUNCH_TASKS.items()}
        try:
            with connect() as conn:
                _upsert_tasks(conn, payload)
        except Exception as exc:
            # 不静默：任务日志是排查抢实例失败的唯一线索，丢了必须让人看见
            print(f"[launch_manager] 保存任务失败: {exc}", flush=True)


def save_launch_task(task_id: str) -> None:
    """只写单个任务行。

    每次 append 日志都全表 upsert 的话，任务一多、日志一长，单次写事务会
    显著变长——这是 SQLite "database is locked" 的集中来源。
    """
    with TASK_LOCK:
        task = LAUNCH_TASKS.get(task_id)
        if task is None:
            return
        payload = {task_id: task}
        try:
            with connect() as conn:
                _upsert_tasks(conn, payload)
        except Exception as exc:
            print(f"[launch_manager] 保存任务失败: {exc}", flush=True)


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
    save_launch_task(task_id)


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
        # 日志永不截断的话，9999 次尝试的任务能积累几万条，每次追加都是全量 JSON 重写
        task["logs"] = task["logs"][-200:]
        task["updated_at"] = now_iso()
    save_launch_task(task_id)


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
    """创建实例所需的可用域/子网/镜像清单。

    镜像列表（list_images）可能几百条，是实例页最慢的部分之一，挂 TTL 缓存。
    """
    from cache import cached

    tenant_name = tenant_cfg.get("_tenant_name") or tenant_cfg.get("tenant_id", "-")
    try:
        from flask import g

        bypass = bool(getattr(g, "cache_bypass", False))
    except RuntimeError:
        bypass = False
    return cached(
        ("launch_context", tenant_name, compartment_of(tenant_cfg)),
        _launch_context_uncached,
        bypass=bypass,
        args=(tenant_cfg,),
    )


def _launch_context_uncached(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
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
        for subnet in list_all(network_client.list_subnets, compartment_id=compartment_of(tenant_cfg))
    ]
    subnets.sort(key=lambda item: (item["name"] or "", item["availability_domain"]))
    return {
        "availability_domains": availability_domains,
        "subnets": subnets,
        "images": discover_candidate_images(compute_client, compartment_of(tenant_cfg)),
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
        compartment_id=compartment_of(tenant_cfg),
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
    """线程入口：任何未捕获异常都不能让任务卡在 running。"""
    try:
        _launch_worker_impl(task_id, tenant_cfg, form)
    except Exception as exc:
        update_task(task_id, status="failed", last_error=str(exc), finished_at=now_iso())
        append_task_log(task_id, f"任务异常终止：{exc}")
        notify("OCI 创建任务失败", f"{task_id}: {exc}", {"tenant_name": tenant_cfg.get("_tenant_name")})


def _fetch_ssh_hint(tenant_cfg: dict[str, Any], instance_id: str, attempts: int = 4) -> str:
    """抢机成功后等公网 IP 分配，通知里直接给 SSH 命令（最多等 ~40s）。

    任务此时已标记 success：这里任何异常都只降级为提示文案，
    绝不能抛回给 worker 把成功翻转成失败、甚至触发重试重复开机。
    """
    try:
        from compartment_scope import compartment_of
        from oci_helpers import primary_vnic

        network_client = get_network_client(tenant_cfg)
        compartment_id = compartment_of(tenant_cfg)
        for _ in range(attempts):
            try:
                vnic = primary_vnic(network_client, get_compute_client(tenant_cfg), compartment_id, instance_id)
                ip = getattr(vnic, "public_ip", None) if vnic else None
                if ip:
                    return f"SSH: ssh opc@{ip}"
            except Exception:
                pass
            time.sleep(10)
    except Exception:
        pass
    return "公网 IP 尚未分配，稍后到实例总览页查看。"


def _launch_worker_impl(task_id: str, tenant_cfg: dict[str, Any], form: dict[str, Any]) -> None:
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
            ssh_hint = _fetch_ssh_hint(tenant_cfg, instance.id)
            notify(
                "OCI 创建实例成功",
                f"{task_id}: {instance.display_name}\n{ssh_hint}",
                {"tenant_name": tenant_cfg.get("_tenant_name"), "instance_id": instance.id},
            )
            return
        except Exception as exc:
            append_task_log(task_id, f"尝试失败：{exc}")
            if attempt >= attempts or not is_retryable_launch_error(exc):
                update_task(task_id, status="failed", last_error=str(exc), finished_at=now_iso())
                append_task_log(task_id, "任务结束，未再继续重试。")
                notify("OCI 创建实例失败", f"{task_id}: {exc}", {"tenant_name": tenant_cfg.get("_tenant_name")})
                return
            time.sleep(interval_seconds)
