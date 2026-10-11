from __future__ import annotations

import base64
import re
import secrets
import threading
import time
from typing import Any

import oci

from oci_helpers import build_config, client_kwargs, list_all
from settings import MAX_DOWNLOAD_BYTES, MAX_DOWNLOAD_MB
from storage import fmt_dt
from compartment_scope import compartment_of

BUCKET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def get_object_storage_client(tenant_cfg: dict[str, Any]) -> oci.object_storage.ObjectStorageClient:
    from oci_helpers import get_cached_client

    return get_cached_client("object_storage", tenant_cfg, lambda: oci.object_storage.ObjectStorageClient(build_config(tenant_cfg), **client_kwargs()))


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


# namespace 对租户不变：每个对象操作都打一次 get_namespace() 纯属浪费一跳 RTT
_NAMESPACE_CACHE: dict[str, str] = {}

# 大文件分片上传会话：token -> 会话。分片正文不经服务器持久化，
# 只在内存里记录 etag；超过 TTL 的会话在下次 init 时清理。
_MULTIPART_SESSIONS: dict[str, dict[str, Any]] = {}
_MULTIPART_LOCK = threading.Lock()
_MULTIPART_TTL_SECONDS = 24 * 3600
_MULTIPART_CHUNK_MB = 64


def init_multipart(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str) -> dict[str, Any]:
    bucket_name = validate_bucket_name(bucket_name)
    object_name = validate_object_name(object_name)
    client = get_object_storage_client(tenant_cfg)
    ns = _cached_namespace(client, tenant_cfg)
    details = oci.object_storage.models.CreateMultipartUploadDetails(object=object_name)
    upload_id = client.create_multipart_upload(ns, bucket_name, details).data.upload_id
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _MULTIPART_LOCK:
        for key in [k for k, v in _MULTIPART_SESSIONS.items() if now - v["created"] > _MULTIPART_TTL_SECONDS]:
            _MULTIPART_SESSIONS.pop(key, None)  # 遗留会话的分片由 OCI 生命周期/手动清理
        _MULTIPART_SESSIONS[token] = {
            "namespace": ns,
            "bucket": bucket_name,
            "object": object_name,
            "upload_id": upload_id,
            "parts": [],
            "created": now,
            "tenant_id": tenant_cfg.get("tenant_id", ""),
        }
    return {"token": token, "chunk_mb": _MULTIPART_CHUNK_MB, "object": object_name}


def _multipart_session(token: str, tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    with _MULTIPART_LOCK:
        session = _MULTIPART_SESSIONS.get(token)
    if session is None or session.get("tenant_id") != tenant_cfg.get("tenant_id", ""):
        raise ValueError("分片上传会话不存在或已过期，请重新开始上传。")
    return session


def upload_multipart_part(tenant_cfg: dict[str, Any], token: str, part_number: int, body: Any, content_length: int) -> str:
    session = _multipart_session(token, tenant_cfg)
    client = get_object_storage_client(tenant_cfg)
    response = client.upload_part(
        namespace_name=session["namespace"],
        bucket_name=session["bucket"],
        object_name=session["object"],
        upload_id=session["upload_id"],
        upload_part_body=body,
        content_length=content_length,
        part_number=part_number,
    )
    etag = getattr(response.data, "etag", "") or ""
    with _MULTIPART_LOCK:
        session["parts"].append({"part_num": part_number, "etag": etag})
    return etag


def commit_multipart(tenant_cfg: dict[str, Any], token: str) -> dict[str, Any]:
    session = _multipart_session(token, tenant_cfg)
    with _MULTIPART_LOCK:
        parts = sorted(session["parts"], key=lambda item: item["part_num"])
    if not parts:
        raise ValueError("没有已上传的分片，无法合并。")
    client = get_object_storage_client(tenant_cfg)
    commit_details = oci.object_storage.models.CommitMultipartUploadDetails(
        parts_to_commit=[
            oci.object_storage.models.CommitMultipartUploadPartDetails(part_num=item["part_num"], etag=item["etag"])
            for item in parts
        ]
    )
    client.commit_multipart_upload(session["namespace"], session["bucket"], session["object"], session["upload_id"], commit_details)
    with _MULTIPART_LOCK:
        _MULTIPART_SESSIONS.pop(token, None)
    return {"object": session["object"], "parts": len(parts)}


def abort_multipart(tenant_cfg: dict[str, Any], token: str) -> None:
    try:
        session = _multipart_session(token, tenant_cfg)
    except ValueError:
        return
    client = get_object_storage_client(tenant_cfg)
    try:
        client.abort_multipart_upload(
            namespace_name=session["namespace"],
            bucket_name=session["bucket"],
            object_name=session["object"],
            upload_id=session["upload_id"],
        )
    finally:
        with _MULTIPART_LOCK:
            _MULTIPART_SESSIONS.pop(token, None)


def delete_objects_under_prefix(tenant_cfg: dict[str, Any], bucket_name: str, prefix: str, max_requests: int = 100) -> int:
    """按前缀批量删除对象（每请求最多 100 个，总上限 100 页防失控）。"""
    client = get_object_storage_client(tenant_cfg)
    ns = _cached_namespace(client, tenant_cfg)
    deleted = 0
    start: str | None = None
    for _ in range(max_requests):
        response = client.list_objects(
            namespace_name=ns,
            bucket_name=bucket_name,
            prefix=prefix or None,
            limit=1000,
            fields="name",
            **({"start": start} if start else {}),
        )
        names = [obj.name for obj in getattr(response.data, "objects", []) or []]
        for i in range(0, len(names), 100):
            batch = names[i : i + 100]
            delete_details = oci.object_storage.models.BatchDeleteObjectsDetails(
                objects=[{"name": name} for name in batch]
            )
            client.delete_objects(ns, bucket_name, delete_details)
            deleted += len(batch)
        start = getattr(response.data, "next_start_with", None) or None
        if not start or not names:
            break
    return deleted


def _cached_namespace(client: Any, tenant_cfg: dict[str, Any]) -> str:
    key = tenant_cfg.get("tenant_id", "-")
    ns = _NAMESPACE_CACHE.get(key)
    if not ns:
        ns = str(client.get_namespace().data)
        _NAMESPACE_CACHE[key] = ns
    return ns


def storage_context(tenant_cfg: dict[str, Any], namespace: str | None = None, bucket_name: str | None = None, prefix: str = "") -> dict[str, Any]:
    client = get_object_storage_client(tenant_cfg)
    namespace_name = namespace or _cached_namespace(client, tenant_cfg)
    buckets = list_all(client.list_buckets, namespace_name=namespace_name, compartment_id=compartment_of(tenant_cfg))
    bucket_rows = sorted((_bucket_row(bucket) for bucket in buckets), key=lambda item: item["name"].lower())

    selected_bucket = None
    object_rows: list[dict[str, Any]] = []
    objects_truncated = False
    if bucket_name:
        bucket = client.get_bucket(namespace_name=namespace_name, bucket_name=bucket_name).data
        selected_bucket = _bucket_detail(bucket)
        # 分页拉全量：list_objects 单页最多约 1000 条，之前只取第一页，
        # 大 bucket 的列表和统计都会静默截断
        start: str | None = None
        for _page in range(20):  # 2 万条封顶，防异常 bucket 拖死页面
            response = client.list_objects(
                namespace_name=namespace_name,
                bucket_name=bucket_name,
                prefix=prefix or None,
                fields="name,size,timeCreated,md5,timeModified,storageTier,etag",
                limit=1000,
                **({"start": start} if start else {}),
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
            start = getattr(response.data, "next_start_with", None) or None
            if not start:
                break
        else:
            objects_truncated = True
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
        "objects_truncated": objects_truncated,
        "mount_helper": mount_helper,
    }


def create_bucket(tenant_cfg: dict[str, Any], bucket_name: str, storage_tier: str) -> None:
    bucket_name = validate_bucket_name(bucket_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = _cached_namespace(client, tenant_cfg)
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
    namespace_name = _cached_namespace(client, tenant_cfg)
    client.put_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name, put_object_body=file_obj.stream)


def create_folder_marker(tenant_cfg: dict[str, Any], bucket_name: str, folder_name: str) -> str:
    bucket_name = validate_bucket_name(bucket_name)
    object_name = validate_object_name(folder_name)
    if not object_name.endswith("/"):
        object_name += "/"
    client = get_object_storage_client(tenant_cfg)
    namespace_name = _cached_namespace(client, tenant_cfg)
    client.put_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name, put_object_body=b"")
    return object_name


def download_object(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str) -> tuple[bytes, str]:
    bucket_name = validate_bucket_name(bucket_name)
    object_name = validate_object_name(object_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = _cached_namespace(client, tenant_cfg)
    # 先问大小再决定是否下载：对象内容会整体进内存，不设门槛的话
    # 一个几十 GB 的对象就能把进程打爆。
    head = client.head_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name)
    size = int(head.headers.get("content-length", "0") or "0")
    if size > MAX_DOWNLOAD_BYTES:
        raise ValueError(
            f"对象过大（{size / 1024 / 1024:.1f} MB），超过 {MAX_DOWNLOAD_MB} MB 的下载上限，请改用 OCI CLI 或 rclone 取回。"
        )
    response = client.get_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name)
    return response.data.content, response.headers.get("content-type", "application/octet-stream")


def preview_object(tenant_cfg: dict[str, Any], bucket_name: str, object_name: str) -> dict[str, Any]:
    bucket_name = validate_bucket_name(bucket_name)
    object_name = validate_object_name(object_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = _cached_namespace(client, tenant_cfg)
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
    namespace_name = _cached_namespace(client, tenant_cfg)
    client.delete_object(namespace_name=namespace_name, bucket_name=bucket_name, object_name=object_name)


def delete_bucket(tenant_cfg: dict[str, Any], bucket_name: str) -> None:
    bucket_name = validate_bucket_name(bucket_name)
    client = get_object_storage_client(tenant_cfg)
    namespace_name = _cached_namespace(client, tenant_cfg)
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
    namespace_name = _cached_namespace(client, tenant_cfg)
    client.update_bucket(
        namespace_name=namespace_name,
        bucket_name=bucket_name,
        update_bucket_details=oci.object_storage.models.UpdateBucketDetails(
            public_access_type=public_access_type,
            versioning=versioning,
            auto_tiering=auto_tiering,
        ),
    )

