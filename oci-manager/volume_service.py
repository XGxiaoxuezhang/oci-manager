"""块存储：卷 / 引导卷 / 卷备份的查看与管理。

之前 cost 与 idle 两个服务已经在遍历 boot volume 和 volume 做闲置与额度检测，
但没有任何页面能看见它们——"能发现问题，管不到"。这里把操作面补上。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import oci

from compartment_scope import compartment_of
from oci_helpers import get_block_client, get_compute_client, get_identity_client, list_all
from storage import fmt_dt, now_iso

_ATTACHABLE_STATES = {"RUNNING", "STOPPED"}


def _safe(future, default):
    try:
        return future.result()
    except Exception:
        return default


def _availability_domains(tenant_cfg: dict[str, Any]) -> list[str]:
    identity = get_identity_client(tenant_cfg)
    # AD 是 tenancy 级资源，不随 compartment 变化
    return [ad.name for ad in list_all(identity.list_availability_domains, compartment_id=tenant_cfg["tenant_id"])]


def instance_choices(tenant_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """可供挂载的实例列表（已终止的实例排除）。"""
    compute = get_compute_client(tenant_cfg)
    compartment = compartment_of(tenant_cfg)
    ads = _availability_domains(tenant_cfg)
    rows: list[dict[str, Any]] = []
    if not ads:
        return rows
    with ThreadPoolExecutor(max_workers=min(8, len(ads))) as pool:
        futures = [
            pool.submit(list_all, compute.list_instances, compartment_id=compartment, availability_domain=ad) for ad in ads
        ]
        for future in futures:
            for instance in _safe(future, []):
                state = getattr(instance, "lifecycle_state", "")
                if state not in _ATTACHABLE_STATES:
                    continue
                rows.append({"id": instance.id, "name": getattr(instance, "display_name", "") or instance.id, "state": state})
    rows.sort(key=lambda item: item["name"].lower())
    return rows


def volume_context(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    """卷 / 引导卷 / 备份 / 挂载关系，并发拉取后挂 TTL 缓存。"""
    from cache import tenant_cached

    return tenant_cached(tenant_cfg, "volume_context", _volume_context_uncached)


def _volume_context_uncached(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    compute = get_compute_client(tenant_cfg)
    block = get_block_client(tenant_cfg)
    compartment = compartment_of(tenant_cfg)
    ads = _availability_domains(tenant_cfg)

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_volumes = pool.submit(list_all, block.list_volumes, compartment_id=compartment)
        f_backups = pool.submit(list_all, block.list_volume_backups, compartment_id=compartment)
        f_attachments = pool.submit(list_all, compute.list_volume_attachments, compartment_id=compartment)
        f_boot_volumes = [
            pool.submit(list_all, block.list_boot_volumes, ad, compartment_id=compartment) for ad in ads
        ]
        f_boot_attachments = [
            pool.submit(list_all, compute.list_boot_volume_attachments, ad, compartment_id=compartment) for ad in ads
        ]

        volumes = _safe(f_volumes, [])
        backups = _safe(f_backups, [])
        attachments = _safe(f_attachments, [])
        boot_volumes: list[Any] = []
        for future in f_boot_volumes:
            boot_volumes.extend(_safe(future, []))
        boot_attachments: list[Any] = []
        for future in f_boot_attachments:
            boot_attachments.extend(_safe(future, []))

    instance_names: dict[str, str] = {}
    choices = instance_choices(tenant_cfg)
    for choice in choices:
        instance_names[choice["id"]] = choice["name"]

    attach_by_volume: dict[str, dict[str, Any]] = {}
    for item in attachments:
        attach_by_volume[item.volume_id] = {
            "attachment_id": item.id,
            "instance_id": getattr(item, "instance_id", "") or "",
            "instance_name": instance_names.get(getattr(item, "instance_id", "") or "", "未知实例"),
            "state": getattr(item, "lifecycle_state", "-"),
            "type": getattr(item, "attachment_type", "-"),
        }

    boot_attach_by_volume: dict[str, dict[str, Any]] = {}
    for item in boot_attachments:
        boot_attach_by_volume[item.boot_volume_id] = {
            "attachment_id": item.id,
            "instance_id": getattr(item, "instance_id", "") or "",
            "instance_name": instance_names.get(getattr(item, "instance_id", "") or "", "未知实例"),
            "state": getattr(item, "lifecycle_state", "-"),
        }

    volume_rows = [
        {
            "id": volume.id,
            "name": getattr(volume, "display_name", "") or "-",
            "size_gbs": getattr(volume, "size_in_gbs", None) or getattr(volume, "size_in_mbs", 0) // 1024,
            "state": getattr(volume, "lifecycle_state", "-"),
            "availability_domain": getattr(volume, "availability_domain", "-"),
            "attached": attach_by_volume.get(volume.id),
            "created": fmt_dt(getattr(volume, "time_created", "")),
        }
        for volume in volumes
        if str(getattr(volume, "lifecycle_state", "")) != "TERMINATED"
    ]
    volume_rows.sort(key=lambda item: (item["attached"] is None, item["name"].lower()))

    boot_rows = [
        {
            "id": volume.id,
            "name": getattr(volume, "display_name", "") or "-",
            "size_gbs": getattr(volume, "size_in_gbs", 0),
            "state": getattr(volume, "lifecycle_state", "-"),
            "availability_domain": getattr(volume, "availability_domain", "-"),
            "attached": boot_attach_by_volume.get(volume.id),
            "created": fmt_dt(getattr(volume, "time_created", "")),
        }
        for volume in boot_volumes
        if str(getattr(volume, "lifecycle_state", "")) != "TERMINATED"
    ]
    boot_rows.sort(key=lambda item: (item["attached"] is None, item["name"].lower()))

    volume_names = {row["id"]: row["name"] for row in volume_rows}
    backup_rows = [
        {
            "id": backup.id,
            "name": getattr(backup, "display_name", "") or "-",
            "volume_name": volume_names.get(getattr(backup, "volume_id", ""), "（源卷已删除）"),
            "volume_id": getattr(backup, "volume_id", ""),
            "size_gbs": getattr(backup, "size_in_gbs", None) or getattr(backup, "size_in_mbs", 0) // 1024,
            "type": getattr(backup, "type", "-"),
            "state": getattr(backup, "lifecycle_state", "-"),
            "created": fmt_dt(getattr(backup, "time_created", "")),
        }
        for backup in backups
        if str(getattr(backup, "lifecycle_state", "")) != "TERMINATED"
    ]
    backup_rows.sort(key=lambda item: item["created"], reverse=True)

    return {
        "volumes": volume_rows,
        "boot_volumes": boot_rows,
        "backups": backup_rows,
        "instances": choices,
        "availability_domains": ads,
        "orphan_count": sum(1 for row in volume_rows + boot_rows if row["attached"] is None),
    }


def create_volume(tenant_cfg: dict[str, Any], availability_domain: str, size_gbs: int, display_name: str) -> str:
    block = get_block_client(tenant_cfg)
    name = (display_name or "").strip() or f"volume-{now_iso()[:10]}"
    volume = block.create_volume(
        oci.core.models.CreateVolumeDetails(
            compartment_id=compartment_of(tenant_cfg),
            availability_domain=availability_domain,
            display_name=name,
            size_in_gbs=size_gbs,
        )
    ).data
    return volume.id


def attach_volume(tenant_cfg: dict[str, Any], volume_id: str, instance_id: str) -> None:
    compute = get_compute_client(tenant_cfg)
    compute.attach_volume(
        oci.core.models.AttachParavirtualizedVolumeDetails(
            type="paravirtualized",
            instance_id=instance_id,
            volume_id=volume_id,
            display_name=f"attachment-{now_iso()[:10]}",
        )
    )


def detach_volume(tenant_cfg: dict[str, Any], attachment_id: str) -> None:
    get_compute_client(tenant_cfg).detach_volume(attachment_id)


def resize_volume(tenant_cfg: dict[str, Any], volume_id: str, size_gbs: int) -> None:
    block = get_block_client(tenant_cfg)
    block.update_volume(volume_id, oci.core.models.UpdateVolumeDetails(size_in_gbs=size_gbs))


def delete_volume(tenant_cfg: dict[str, Any], volume_id: str) -> None:
    get_block_client(tenant_cfg).delete_volume(volume_id)


def resize_boot_volume(tenant_cfg: dict[str, Any], boot_volume_id: str, size_gbs: int) -> None:
    block = get_block_client(tenant_cfg)
    block.update_boot_volume(boot_volume_id, oci.core.models.UpdateBootVolumeDetails(size_in_gbs=size_gbs))


def delete_boot_volume(tenant_cfg: dict[str, Any], boot_volume_id: str) -> None:
    get_block_client(tenant_cfg).delete_boot_volume(boot_volume_id)


def create_backup(tenant_cfg: dict[str, Any], volume_id: str, display_name: str = "") -> None:
    block = get_block_client(tenant_cfg)
    block.create_volume_backup(
        oci.core.models.CreateVolumeBackupDetails(
            volume_id=volume_id,
            display_name=(display_name or "").strip() or f"backup-{now_iso()[:19].replace(':', '')}",
            type="FULL",
        )
    )


def restore_backup(tenant_cfg: dict[str, Any], backup_id: str, availability_domain: str, display_name: str = "") -> str:
    """从备份创建一个**新卷**，而不是覆盖原卷——覆盖是不可逆的。"""
    block = get_block_client(tenant_cfg)
    volume = block.create_volume(
        oci.core.models.CreateVolumeDetails(
            compartment_id=compartment_of(tenant_cfg),
            availability_domain=availability_domain,
            display_name=(display_name or "").strip() or f"restored-{now_iso()[:19].replace(':', '')}",
            source_details=oci.core.models.VolumeSourceFromVolumeBackupDetails(type="volumeBackup", id=backup_id),
        )
    ).data
    return volume.id


def delete_backup(tenant_cfg: dict[str, Any], backup_id: str) -> None:
    get_block_client(tenant_cfg).delete_volume_backup(backup_id)
