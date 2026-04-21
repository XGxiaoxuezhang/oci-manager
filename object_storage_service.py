from __future__ import annotations

import base64
from typing import Any

import oci

from oci_helpers import build_config, client_kwargs, list_all
from storage import fmt_dt


def get_object_storage_client(tenant_cfg: dict[str, Any]) -> oci.object_storage.ObjectStorageClient:
    return oci.object_storage.ObjectStorageClient(build_config(tenant_cfg), **client_kwargs())


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
    buckets = list_all(client.list_buckets, namespace_name=namespace_name, compartment_id=tenant_cfg["tenant_id"])
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
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    client.create_bucket(
        namespace_name=namespace_name,
        create_bucket_details=oci.object_storage.models.CreateBucketDetails(
            compartment_id=tenant_cfg["tenant_id"],
            name=bucket_name,
            storage_tier=storage_tier,
            public_access_type="NoPublicAccess",
        ),
    )


def upload_object(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str, file_obj: Any) -> None:
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    client.put_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name, put_object_body=file_obj.stream.read())


def download_object(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str) -> tuple[bytes, str]:
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    response = client.get_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name)
    return response.data.content, response.headers.get("content-type", "application/octet-stream")


def preview_object(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str) -> dict[str, Any]:
    client = get_object_storage_client(tenant_cfg)
    namespace_name = client.get_namespace().data
    response = client.get_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name)
    payload = response.data.content
    content_type = response.headers.get("content-type", "application/octet-stream")
    preview = {"mode": "binary", "text": "", "data_url": "", "truncated": False}
    limited_payload = payload[:200000]
    preview["truncated"] = len(payload) > len(limited_payload)
    if content_type.startswith("text/") or content_type in {"application/json", "application/xml", "application/yaml", "text/csv"}:
        preview["mode"] = "text"
        preview["text"] = limited_payload.decode("utf-8", errors="replace")
    elif content_type.startswith("image/"):
        preview["mode"] = "image"
        encoded = base64.b64encode(limited_payload).decode("ascii")
        preview["data_url"] = f"data:{content_type};base64,{encoded}"
    return {
        "object_name": object_name,
        "bucket_name": bucket_name,
        "content_type": content_type,
        "size": len(payload),
        "etag": response.headers.get("etag", ""),
        "preview": preview,
    }

