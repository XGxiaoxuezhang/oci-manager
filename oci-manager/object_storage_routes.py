from __future__ import annotations

from io import BytesIO

from oci_helpers import friendly_error
from flask import Blueprint, flash, redirect, request, send_file, url_for

from export_utils import send_csv
from oci_helpers import build_dashboard_cards
from object_storage_service import (
    abort_multipart,
    commit_multipart,
    create_bucket,
    create_folder_marker,
    delete_bucket,
    delete_objects_under_prefix,
    delete_object,
    download_object,
    init_multipart,
    preview_object,
    storage_context,
    update_bucket_settings,
    upload_multipart_part,
    upload_object,
)
from rendering import render_page
from storage import load_tenants
from tenant_routes import require_tenant
from timeout_utils import run_with_timeout

object_storage_bp = Blueprint("object_storage", __name__)


@object_storage_bp.route("/object-storage")
def object_storage_overview():
    tenants = load_tenants()
    if tenants:
        return redirect(url_for("object_storage.object_storage_home", tenant_name=sorted(tenants.keys())[0]))
    return render_page("object_storage_overview", module_title="对象存储", module_path="object-storage", tenants=build_dashboard_cards(tenants))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage")
def object_storage_home(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.args.get("bucket") or None
    prefix = request.args.get("prefix", "").strip()
    try:
        context = run_with_timeout(8, storage_context, tenant_cfg, None, bucket_name, prefix)
        return render_page("object_storage", tenant_name=tenant_name, **context)
    except Exception as exc:
        flash(f"读取对象存储失败: {friendly_error(exc)}", "error")
        return render_page(
            "object_storage",
            tenant_name=tenant_name,
            namespace_name="-",
            buckets=[],
            selected_bucket=None,
            selected_bucket_detail=None,
            objects=[],
            object_prefix=prefix,
            storage_stats={"bucket_count": 0, "object_count": 0, "total_size": 0},
            selected_stats={"object_count": 0, "total_size": 0},
            mount_helper={"region": tenant_cfg.get("region", "-"), "namespace": "-", "s3_endpoint": "-", "rclone_remote": "oci", "bucket": "<bucket>"},
            load_error=str(exc),
        )


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/export")
def object_storage_export(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.args.get("bucket") or None
    prefix = request.args.get("prefix", "").strip()
    try:
        context = run_with_timeout(12, storage_context, tenant_cfg, None, bucket_name, prefix)
        if bucket_name:
            rows = context["objects"]
            headers = ["name", "size", "storage_tier", "created", "etag"]
            filename = f"{tenant_name}-{bucket_name}-objects.csv"
        else:
            rows = context["buckets"]
            headers = ["name", "storage_tier", "count", "size", "created"]
            filename = f"{tenant_name}-buckets.csv"
        return send_csv(filename, headers, rows)
    except Exception as exc:
        flash(f"导出对象存储失败: {friendly_error(exc)}", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None, prefix=prefix or None))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/rclone.conf")
def object_storage_rclone_config(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.args.get("bucket") or "<bucket>"
    try:
        context = run_with_timeout(8, storage_context, tenant_cfg, None, None, "")
        helper = context["mount_helper"]
        remote = helper["rclone_remote"]
        text = "\n".join(
            [
                f"[{remote}]",
                "type = s3",
                "provider = Other",
                "env_auth = false",
                "access_key_id = <OCI access key>",
                "secret_access_key = <OCI secret key>",
                f"endpoint = {helper['s3_endpoint']}",
                "",
                f"# mount: rclone mount {remote}:{bucket_name} X:\\object-storage",
                "",
            ]
        )
        return send_file(
            BytesIO(text.encode("utf-8")),
            as_attachment=True,
            download_name=f"{tenant_name}-rclone.conf",
            mimetype="text/plain; charset=utf-8",
        )
    except Exception as exc:
        flash(f"生成 rclone 配置失败: {friendly_error(exc)}", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name if bucket_name != "<bucket>" else None))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/bucket", methods=["POST"])
def object_storage_create_bucket(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.form.get("bucket_name", "").strip()
    try:
        create_bucket(tenant_cfg, bucket_name, request.form.get("storage_tier", "Standard"))
        flash(f"Bucket {bucket_name} 已创建。", "success")
    except Exception as exc:
        flash(f"创建 Bucket 失败: {friendly_error(exc)}", "error")
    return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/bucket/delete", methods=["POST"])
def object_storage_delete_bucket(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.form.get("bucket_name", "").strip()
    confirm_name = request.form.get("confirm_name", "").strip()
    if confirm_name != bucket_name:
        flash("确认名称不匹配，未删除 Bucket。", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))
    try:
        delete_bucket(tenant_cfg, bucket_name)
        flash(f"Bucket {bucket_name} 已删除。", "success")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name))
    except Exception as exc:
        flash(f"删除 Bucket 失败: {friendly_error(exc)}", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/bucket/settings", methods=["POST"])
def object_storage_bucket_settings(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.form.get("bucket_name", "").strip()
    public_access_type = request.form.get("public_access_type", "NoPublicAccess")
    versioning = request.form.get("versioning", "Suspended")
    auto_tiering = request.form.get("auto_tiering", "Disabled")
    confirm_name = request.form.get("confirm_name", "").strip()
    if public_access_type != "NoPublicAccess" and confirm_name != bucket_name:
        flash("开启公共访问需要输入 Bucket 名称确认。", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))
    try:
        update_bucket_settings(tenant_cfg, bucket_name, public_access_type, versioning, auto_tiering)
        flash("Bucket 设置已更新。", "success")
    except Exception as exc:
        flash(f"更新 Bucket 设置失败: {friendly_error(exc)}", "error")
    return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/multipart/init", methods=["POST"])
def multipart_init(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return {"ok": False, "message": "租户不存在。"}
    try:
        info = init_multipart(
            tenant_cfg,
            request.form.get("bucket_name", "").strip(),
            request.form.get("object_name", "").strip(),
        )
        return {"ok": True, **info}
    except Exception as exc:
        return {"ok": False, "message": friendly_error(exc)}


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/multipart/part", methods=["POST"])
def multipart_part(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return {"ok": False, "message": "租户不存在。"}
    part_file = request.files.get("part_file")
    try:
        part_number = int(request.form.get("part_number", "0"))
    except ValueError:
        return {"ok": False, "message": "分片序号不正确。"}
    if not part_file or part_number < 1:
        return {"ok": False, "message": "缺少分片内容或序号。"}
    part_file.seek(0, 2)
    content_length = part_file.tell()
    part_file.seek(0)
    try:
        etag = upload_multipart_part(tenant_cfg, request.form.get("token", ""), part_number, part_file, content_length)
        return {"ok": True, "etag": etag}
    except Exception as exc:
        return {"ok": False, "message": friendly_error(exc)}


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/multipart/complete", methods=["POST"])
def multipart_complete(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return {"ok": False, "message": "租户不存在。"}
    try:
        result = commit_multipart(tenant_cfg, request.form.get("token", ""))
        return {"ok": True, "object": result["object"], "parts": result["parts"]}
    except Exception as exc:
        return {"ok": False, "message": friendly_error(exc)}


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/multipart/abort", methods=["POST"])
def multipart_abort(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return {"ok": False, "message": "租户不存在。"}
    abort_multipart(tenant_cfg, request.form.get("token", ""))
    return {"ok": True}


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/prefix-delete", methods=["POST"])
def object_storage_prefix_delete(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.form.get("bucket_name", "").strip()
    prefix = request.form.get("prefix", "").strip()
    confirm = request.form.get("confirm_name", "").strip()
    if not bucket_name or not prefix:
        flash("需要指定 Bucket 和要删除的前缀（不支持清空整个 Bucket）。", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))
    if confirm != prefix:
        flash("确认输入与前缀不一致，未删除。", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name))
    try:
        deleted = delete_objects_under_prefix(tenant_cfg, bucket_name, prefix)
        flash(f"已删除前缀 {prefix} 下的 {deleted} 个对象。", "success")
    except Exception as exc:
        flash(f"按前缀删除失败: {friendly_error(exc)}", "error")
    return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/object", methods=["POST"])
def object_storage_upload(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.form.get("bucket_name", "").strip()
    file_obj = request.files.get("object_file")
    object_name = request.form.get("object_name", "").strip()
    if not bucket_name or not file_obj or not file_obj.filename:
        flash("请选择 Bucket 和要上传的文件。", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))
    try:
        upload_object(tenant_cfg, bucket_name, object_name or file_obj.filename, file_obj)
        flash("对象已上传。", "success")
    except Exception as exc:
        flash(f"上传对象失败: {friendly_error(exc)}", "error")
    return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/folder", methods=["POST"])
def object_storage_create_folder(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.form.get("bucket_name", "").strip()
    folder_name = request.form.get("folder_name", "").strip()
    try:
        created = create_folder_marker(tenant_cfg, bucket_name, folder_name)
        flash(f"目录 {created} 已创建。", "success")
    except Exception as exc:
        flash(f"创建目录失败: {friendly_error(exc)}", "error")
    return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None, prefix=folder_name or None))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/object/delete", methods=["POST"])
def object_storage_delete_object(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.form.get("bucket_name", "").strip()
    object_name = request.form.get("object_name", "").strip()
    try:
        delete_object(tenant_cfg, bucket_name, object_name)
        flash("对象已删除。", "success")
    except Exception as exc:
        flash(f"删除对象失败: {friendly_error(exc)}", "error")
    return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/object/download")
def object_storage_download(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.args.get("bucket", "").strip()
    object_name = request.args.get("name", "").strip()
    if not bucket_name or not object_name:
        flash("Bucket 和对象名称不能为空。", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))
    try:
        payload, content_type = run_with_timeout(12, download_object, tenant_cfg, bucket_name, object_name)
        return send_file(BytesIO(payload), mimetype=content_type, as_attachment=True, download_name=object_name.rsplit("/", 1)[-1] or object_name)
    except Exception as exc:
        flash(f"下载对象失败: {friendly_error(exc)}", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/object/preview")
def object_storage_preview(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.args.get("bucket", "").strip()
    object_name = request.args.get("name", "").strip()
    if not bucket_name or not object_name:
        flash("Bucket 和对象名称不能为空。", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))
    try:
        context = run_with_timeout(12, preview_object, tenant_cfg, bucket_name, object_name)
        return render_page("object_preview", tenant_name=tenant_name, **context)
    except Exception as exc:
        flash(f"预览对象失败: {friendly_error(exc)}", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name))
