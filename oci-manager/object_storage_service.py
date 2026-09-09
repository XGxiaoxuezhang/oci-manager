from __future__ import annotations

import base64
import re
from typing import Any

import oci

from oci_helpers import build_config, client_kwargs, list_all
from storage import fmt_dt
from compartment_scope import compartment_of

BUCKET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def get_object_storage_client(tenant_cfg: dict[str, Any]) -> oci.object_storage.ObjectStorageClient:
    return oci.object_storage.ObjectStorageClient(build_config(tenant_cfg), **client_kwargs())


def validate_bucket_name(bucket_name: str) -> str:
    normalized = (bucket_name or "").strip()
    if not normalized:
        raise ValueError("Bucket 名称不能为空。")
    if "/" in normalized or "\\" in normalized or not BUCKET_NAME_RE.match(normalized):
        raise ValueError("Bucket 名称只能包含字母、数字、点、下划线和中划线，并且不能以符号开头。")
    return normalized


def validate_object_name(object_name: str) -> str:
    normalized = (object_name or "").strip().lstrip("/")
    if not normalized:
        raise ValueError("对象名称不能为空。")
    if "\x00" in normalized or normalized in {".", ".."}:
        raise ValueError("对象名称不合法。")
    return normalized


def _bucket_row(bucket: Any) -> dict[str, Any]:
    return {
        "name": bucket.name,
        "created": fmt_dt(getattr(bucket, "time_created", None)),
        "storage_tier": getattr(bucket, "storage_tier", "-"),
        "count": getattr(bucket, "approximate_count", 0),
        "size": getattr(bucket, "approximate_size", 0),
    }


def _bucket_detail(bucket: Any) -> dict[str, Any]:
    return {
        "name": bucket.name,
        "created": fmt_dt(getattr(bucket, "time_created", None)),
        "compartment_id": getattr(bucket, "compartment_id", ""),
        "namespace": getattr(bucket, "namespace", ""),
        "storage_tier": getattr(bucket, "storage_tier", "-"),
        "public_access_type": getattr(bucket, "public_access_type", "-"),
        "versioning": getattr(bucket, "versioning", "Disabled"),
        "auto_tiering": getattr(bucket, "auto_tiering", "Disabled"),
        "count": getattr(bucket, "approximate_count", 0),
        "size": getattr(bucket, "approximate_size", 0),
    }


def storage_context(tenant_cfg: dict[str, Any], namespace: str | None = None, bucket_name: str | None = None, prefix: str = "") -> dict[str, Any]:
    client = get_object_storage_client(tenant_cfg)
    namespace_name = namespace or client.get_namespace().data
    buckets = list_all(client.list_buckets, namespace_name=namespace_name, compartment_id=compartment_of(tenant_cfg))
    bucket_rows = sorted((_bucket_row(bucket) for bucket in buckets), key=lambda item: item["name"].lower())

    selected_bucket = None
    object_rows: list[dict[str, Any]] = []
    if bucket_name:
        bucket = client.get_bucket(namespace_name=namespace_name, bucket_name=bucket_name).data
        selected_bucket = _bucket_detail(bucket)
        response = client.list_objects(
            namespace_name=namespace_name,
            bucket_name=bucket_name,
            prefix=prefix or None,
            fields="name,size,timeCreated,md5,timeModified,storageTier,etag",
        )
        for item in getattr(response.data, "objects", []) or []:
            object_rows.append(
                {
                    "name": item.name,
                    "size": getattr(item, "size", 0),
                    "storage_tier": getattr(item, "storage_tier", "-"),
                    "created": fmt_dt(getattr(item, "time_created", None) or getattr(item, "time_modified", None)),
                    "etag": getattr(item, "etag", ""),
                }
            )
        object_rows.sort(key=lambda item: item["name"].lower())

    total_size = sum(bucket["size"] or 0 for bucket in bucket_rows)
    total_count = sum(bucket["count"] or 0 for bucket in bucket_rows)
    selected_stats = {
        "object_count": len(object_rows),
        "total_size": sum(item["size"] or 0 for item in object_rows),
    }
    region = tenant_cfg.get("region", "")
    mount_helper = {
        "namespace": namespace_name,
        "bucket": bucket_name or "<bucket>",
        "region": region,
        "s3_endpoint": f"https://{namespace_name}.compat.objectstorage.{region}.oraclecloud.com",
        "rclone_remote": f"oci-{tenant_cfg.get('_tenant_name', 'tenant')}",
    }
    return {
        "namespace_name": namespace_name,
        "buckets": bucket_rows,
        "selected_bucket": bucket_name,
        "selected_bucket_detail": selected_bucket,
        "objects": object_rows,
        "object_prefix": prefix,
        "storage_stats": {
            "bucket_count": len(bucket_rows),
            "object_count": total_count,
            "total_size": total_size,
        },
        "selected_stats": selected_stats,
        "mount_helper": mount_helper,
    }


def create_bucket(tenant_cfg: dict[str, Any], bucket_name: str, storage_tier: str) -> None:
    bucket_name = validate_bucket_name(bucket_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    client.create_bucket(
        namespace_name=namespace_name,
        create_bucket_details=oci.object_storage.models.CreateBucketDetails(
            compartment_id=compartment_of(tenant_cfg),
            name=bucket_name,
            storage_tier=storage_tier,
            public_access_type="NoPublicAccess",
        ),
    )


def upload_object(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str, file_obj: Any) -> None:
    bucket_name = validate_bucket_name(bucket_name)
    object_name = validate_object_name(object_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    client.put_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name, put_object_body=file_obj.stream)


def create_folder_marker(tenant_cfg: dict[str, Any], bucket_name: str, folder_name: str) -> str:
    bucket_name = validate_bucket_name(bucket_name)
    object_name = validate_object_name(folder_name)
    if not object_name.endswith("/"):
        object_name += "/"
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    client.put_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name, put_object_body=b"")
    return object_name


def download_object(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str) -> tuple[bytes, str]:
    bucket_name = validate_bucket_name(bucket_name)
    object_name = validate_object_name(object_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    response = client.get_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name)
    return response.data.content, response.headers.get("content-type", "application/octet-stream")


def preview_object(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str) -> dict[str, Any]:
    bucket_name = validate_bucket_name(bucket_name)
    object_name = validate_object_name(object_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    head = client.head_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name)
    size = int(head.headers.get("content-length", "0") or "0")
    content_type = head.headers.get("content-type", "application/octet-stream")
    response = client.get_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name, range="bytes=0-199999")
    payload = response.data.content
    preview = {"mode": "binary", "text": "", "data_url": "", "truncated": False}
    limited_payload = payload[:200000]
    preview["truncated"] = size > len(limited_payload)
    if content_type.startswith("text/") or content_type in {"application/json", "application/xml", "application/yaml", "text/csv"}:
        preview["mode"] = "text"
        preview["text"] = limited_payload.decode("utf-8", errors="replace")
    elif content_type.startswith("image/") and not preview["truncated"]:
        preview["mode"] = "image"
        encoded = base64.b64encode(limited_payload).decode("ascii")
        preview["data_url"] = f"data:{content_type};base64,{encoded}"
    return {
        "object_name": object_name,
        "bucket_name": bucket_name,
        "content_type": content_type,
        "size": size,
        "etag": head.headers.get("etag", ""),
        "preview": preview,
    }


def delete_object(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str) -> None:
    bucket_name = validate_bucket_name(bucket_name)
    object_name = validate_object_name(object_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    client.delete_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name)


def delete_bucket(tenant_cfg: dict[str, Any], bucket_name: str) -> None:
    bucket_name = validate_bucket_name(bucket_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    response = client.list_objects(namespace_name=namespace_name, bucket_name=bucket_name, limit=1)
    if getattr(response.data, "objects", None):
        raise ValueError("Bucket 不为空，先删除对象后再删除 Bucket。")
    client.delete_bucket(namespace_name=namespace_name, bucket_name=bucket_name)


def update_bucket_settings(
    tenant_cfg: dict[str, Any],
    bucket_name: str,
    public_access_type: str,
    versioning: str,
    auto_tiering: str,
) -> None:
    bucket_name = validate_bucket_name(bucket_name)
    allowed_public = {"NoPublicAccess", "ObjectRead", "ObjectReadWithoutList"}
    allowed_versioning = {"Enabled", "Suspended"}
    allowed_auto_tiering = {"Disabled", "InfrequentAccess"}
    if public_access_type not in allowed_public:
        raise ValueError("公共访问类型不正确。")
    if versioning not in allowed_versioning:
        raise ValueError("版本控制值不正确。")
    if auto_tiering not in allowed_auto_tiering:
        raise ValueError("自动分层值不正确。")
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    client.update_bucket(
        namespace_name=namespace_name,
        bucket_name=bucket_name,
        update_bucket_details=oci.object_storage.models.UpdateBucketDetails(
            public_access_type=public_access_type,
            versioning=versioning,
            auto_tiering=auto_tiering,
        ),
    )

