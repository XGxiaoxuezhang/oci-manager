from __future__ import annotations

from io import BytesIO

from flask import Blueprint, flash, redirect, request, send_file, url_for

from database_service import (
    create_autonomous_database,
    delete_autonomous_database,
    generate_wallet,
    list_autonomous_databases_context,
    start_autonomous_database,
    stop_autonomous_database,
)
from rendering import render_page
from tenant_routes import require_tenant
from timeout_utils import run_with_timeout

database_bp = Blueprint("database", __name__)


@database_bp.route("/tenant/<tenant_name>/databases")
def databases_home(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    database_id = request.args.get("database_id") or None
    try:
        context = run_with_timeout(8, list_autonomous_databases_context, tenant_cfg, database_id)
        return render_page("databases", tenant_name=tenant_name, **context)
    except Exception as exc:
        flash(f"读取自治数据库失败：{exc}", "error")
        return redirect(url_for("tenant.index"))


@database_bp.route("/tenant/<tenant_name>/databases/create", methods=["POST"])
def databases_create(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    display_name = request.form.get("display_name", "").strip()
    db_name = request.form.get("db_name", "").strip()
    admin_password = request.form.get("admin_password", "")
    workload = request.form.get("workload", "OLTP").strip() or "OLTP"
    db_version = request.form.get("db_version", "19c").strip() or "19c"
    subnet_id = request.form.get("subnet_id", "").strip()
    private_endpoint_label = request.form.get("private_endpoint_label", "").strip()
    character_set = request.form.get("character_set", "AL32UTF8").strip() or "AL32UTF8"
    ncharacter_set = request.form.get("ncharacter_set", "AL16UTF16").strip() or "AL16UTF16"
    is_mtls_connection_required = request.form.get("is_mtls_connection_required") == "on"
    is_auto_scaling_enabled = request.form.get("is_auto_scaling_enabled") == "on"
    is_free_tier = request.form.get("is_free_tier") == "on"
    whitelisted_ips = [item.strip() for item in request.form.get("whitelisted_ips", "").splitlines() if item.strip()]
    try:
        cpu_core_count = int(request.form.get("cpu_core_count", "1") or "1")
        storage_size_gbs = int(request.form.get("storage_size_gbs", "20") or "20")
    except ValueError:
        flash("CPU 核数和存储大小必须是整数。", "error")
        return redirect(url_for("database.databases_home", tenant_name=tenant_name))
    if not display_name or not db_name or not admin_password:
        flash("显示名称、数据库名和管理员密码不能为空。", "error")
        return redirect(url_for("database.databases_home", tenant_name=tenant_name))
    try:
        result = create_autonomous_database(
            tenant_cfg,
            display_name=display_name,
            db_name=db_name,
            admin_password=admin_password,
            workload=workload,
            db_version=db_version,
            cpu_core_count=cpu_core_count,
            storage_size_gbs=storage_size_gbs,
            is_free_tier=is_free_tier,
            subnet_id=subnet_id,
            whitelisted_ips=whitelisted_ips,
            character_set=character_set,
            ncharacter_set=ncharacter_set,
            is_mtls_connection_required=is_mtls_connection_required,
            is_auto_scaling_enabled=is_auto_scaling_enabled,
            private_endpoint_label=private_endpoint_label,
        )
        suffix = " 免费层已固定为 1 核 / 20 GB。" if result["is_free_tier"] else ""
        flash(f"数据库 {result['display_name']} 创建请求已提交。{suffix}", "success")
    except Exception as exc:
        flash(f"创建自治数据库失败：{exc}", "error")
    return redirect(url_for("database.databases_home", tenant_name=tenant_name))


@database_bp.route("/tenant/<tenant_name>/databases/<database_id>/wallet", methods=["POST"])
def databases_wallet(tenant_name: str, database_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    wallet_password = request.form.get("wallet_password", "")
    try:
        payload = run_with_timeout(20, generate_wallet, tenant_cfg, database_id, wallet_password)
        filename = f"adb-wallet-{database_id[-8:]}.zip"
        return send_file(BytesIO(payload), as_attachment=True, download_name=filename, mimetype="application/zip")
    except Exception as exc:
        flash(f"下载 Wallet 失败：{exc}", "error")
        return redirect(url_for("database.databases_home", tenant_name=tenant_name, database_id=database_id))


@database_bp.route("/tenant/<tenant_name>/databases/<database_id>/start", methods=["POST"])
def databases_start(tenant_name: str, database_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        start_autonomous_database(tenant_cfg, database_id)
        flash("启动请求已提交。", "success")
    except Exception as exc:
        flash(f"启动自治数据库失败：{exc}", "error")
    return redirect(url_for("database.databases_home", tenant_name=tenant_name, database_id=database_id))


@database_bp.route("/tenant/<tenant_name>/databases/<database_id>/stop", methods=["POST"])
def databases_stop(tenant_name: str, database_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        stop_autonomous_database(tenant_cfg, database_id)
        flash("停止请求已提交。", "success")
    except Exception as exc:
        flash(f"停止自治数据库失败：{exc}", "error")
    return redirect(url_for("database.databases_home", tenant_name=tenant_name, database_id=database_id))


@database_bp.route("/tenant/<tenant_name>/databases/<database_id>/delete", methods=["POST"])
def databases_delete(tenant_name: str, database_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    confirm_name = request.form.get("confirm_name", "").strip().upper()
    expected_name = request.form.get("expected_name", "").strip().upper()
    if not expected_name or confirm_name != expected_name:
        flash("删除前请输入正确的数据库名确认。", "error")
        return redirect(url_for("database.databases_home", tenant_name=tenant_name, database_id=database_id))
    try:
        delete_autonomous_database(tenant_cfg, database_id)
        flash(f"数据库 {expected_name} 删除请求已提交。", "success")
    except Exception as exc:
        flash(f"删除自治数据库失败：{exc}", "error")
    return redirect(url_for("database.databases_home", tenant_name=tenant_name))