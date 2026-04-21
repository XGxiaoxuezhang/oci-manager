from __future__ import annotations

from io import BytesIO

from flask import Blueprint, flash, redirect, request, send_file, url_for

from object_storage_service import create_bucket, download_object, preview_object, storage_context, upload_object
from rendering import render_page
from tenant_routes import require_tenant
from timeout_utils import run_with_timeout

object_storage_bp = Blueprint("object_storage", __name__)


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
        flash(f"Failed to load object storage: {exc}", "error")
        return redirect(url_for("tenant.index"))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/bucket", methods=["POST"])
def object_storage_create_bucket(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.form.get("bucket_name", "").strip()
    if not bucket_name:
        flash("Bucket name is required.", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name))
    try:
        create_bucket(tenant_cfg, bucket_name, request.form.get("storage_tier", "Standard"))
        flash(f"Bucket {bucket_name} created.", "success")
    except Exception as exc:
        flash(f"Failed to create bucket: {exc}", "error")
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
        flash("Select both a bucket and a file to upload.", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))
    try:
        upload_object(tenant_cfg, bucket_name, object_name or file_obj.filename, file_obj)
        flash("Object uploaded.", "success")
    except Exception as exc:
        flash(f"Failed to upload object: {exc}", "error")
    return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/object/download")
def object_storage_download(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.args.get("bucket", "").strip()
    object_name = request.args.get("name", "").strip()
    if not bucket_name or not object_name:
        flash("Bucket and object name are required.", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))
    try:
        payload, content_type = run_with_timeout(12, download_object, tenant_cfg, bucket_name, object_name)
        return send_file(BytesIO(payload), mimetype=content_type, as_attachment=True, download_name=object_name.rsplit("/", 1)[-1] or object_name)
    except Exception as exc:
        flash(f"Failed to download object: {exc}", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name))


@object_storage_bp.route("/tenant/<tenant_name>/object-storage/object/preview")
def object_storage_preview(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    bucket_name = request.args.get("bucket", "").strip()
    object_name = request.args.get("name", "").strip()
    if not bucket_name or not object_name:
        flash("Bucket and object name are required.", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name or None))
    try:
        context = run_with_timeout(12, preview_object, tenant_cfg, bucket_name, object_name)
        return render_page("object_preview", tenant_name=tenant_name, **context)
    except Exception as exc:
        flash(f"Failed to preview object: {exc}", "error")
        return redirect(url_for("object_storage.object_storage_home", tenant_name=tenant_name, bucket=bucket_name))