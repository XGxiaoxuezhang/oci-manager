from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, flash, redirect, request, url_for

from launch_manager import LAUNCH_TASKS, TASK_LOCK, append_task_log, filtered_tasks, launch_context, launch_worker, task_snapshot, update_task
from rendering import render_page
from settings import ALLOWED_KEY_EXTENSIONS, DEFAULT_REGIONS, INSTANCE_ACTIONS, MAX_KEY_SIZE_BYTES
from storage import load_tenants, normalize_tenant_name, now_iso, save_tenants
from tenant_services import (
    append_security_rule,
    change_ip_context,
    create_console,
    create_tenant_record,
    create_user_for_tenant,
    dashboard_context,
    expand_boot_volume,
    list_instance_rows,
    list_security_list_rows,
    list_user_rows,
    remove_security_rule,
    remove_tenant_record,
    replace_public_ip,
    rescue_context,
    reset_user_mfa,
    security_rules_context,
)
from oci_helpers import find_tenant_config, get_compute_client
from timeout_utils import run_with_timeout

tenant_bp = Blueprint("tenant", __name__)


def require_tenant(tenant_name: str):
    tenant_cfg = find_tenant_config(tenant_name)
    if tenant_cfg is None:
        flash(f"租户 {tenant_name} 不存在。", "error")
        return None
    return tenant_cfg


def _validate_key_file(key_file) -> str | None:
    """
    Return error message if the uploaded key file is invalid, else None.
    Checks: extension, size, and basic PEM header.
    """
    filename = key_file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_KEY_EXTENSIONS:
        return f"私钥文件扩展名必须是 {', '.join(ALLOWED_KEY_EXTENSIONS)} 中的一种。"

    # Read into memory to check size & content
    data = key_file.read(MAX_KEY_SIZE_BYTES + 1)
    if len(data) > MAX_KEY_SIZE_BYTES:
        return f"私钥文件过大（最大 {MAX_KEY_SIZE_BYTES // 1024} KB）。"
    if not data:
        return "私钥文件为空。"

    # Basic PEM sanity check
    text = data.decode("utf-8", errors="replace")
    if "-----BEGIN" not in text or "PRIVATE KEY" not in text:
        return "文件内容不像是有效的 PEM 私钥，请检查后重新上传。"

    # Rewind so downstream code can read from stream again
    import io
    key_file.stream = io.BytesIO(data)
    key_file.stream.seek(0)
    return None


@tenant_bp.route("/")
def index():
    return render_page("tenants", **dashboard_context(load_tenants()))


@tenant_bp.route("/tenant/add", methods=["GET", "POST"])
def add_tenant():
    if request.method == "POST":
        tenant_name = normalize_tenant_name(request.form.get("tenant_name", "").strip())
        tenant_id = request.form.get("tenant_id", "").strip()
        user_id = request.form.get("user_id", "").strip()
        region = request.form.get("region", "").strip()
        fingerprint = request.form.get("fingerprint", "").strip()
        key_file = request.files.get("key_file")

        if not tenant_name:
            flash("租户名称不能为空，并且只能包含字母、数字、点、下划线和短横线。", "error")
            return redirect(url_for("tenant.add_tenant"))

        if not key_file or not key_file.filename:
            flash("请上传 OCI API 私钥文件。", "error")
            return redirect(url_for("tenant.add_tenant"))

        # ── Security: validate key file ───────────────────
        err = _validate_key_file(key_file)
        if err:
            flash(err, "error")
            return redirect(url_for("tenant.add_tenant"))

        if not all([tenant_id, user_id, region, fingerprint]):
            flash("租户 ID、用户 ID、区域和指纹均不能为空。", "error")
            return redirect(url_for("tenant.add_tenant"))

        tenants = load_tenants()
        if tenant_name in tenants:
            flash(f"租户 {tenant_name} 已存在，请换一个名称。", "error")
            return redirect(url_for("tenant.add_tenant"))

        create_tenant_record(tenants, tenant_name, tenant_id, user_id, region, fingerprint, key_file)
        save_tenants(tenants)
        flash(f"租户 {tenant_name} 已添加。", "success")
        return redirect(url_for("tenant.index"))
    return render_page("add_tenant", regions=DEFAULT_REGIONS)


@tenant_bp.route("/tenant/<tenant_name>/delete", methods=["POST"])
def delete_tenant(tenant_name: str):
    tenants = load_tenants()
    if tenant_name not in tenants:
        flash(f"租户 {tenant_name} 不存在。", "error")
        return redirect(url_for("tenant.index"))
    remove_tenant_record(tenants, tenant_name)
    save_tenants(tenants)
    flash(f"租户 {tenant_name} 已删除。", "success")
    return redirect(url_for("tenant.index"))


@tenant_bp.route("/tenant/<tenant_name>/users")
def list_users(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        return render_page("users", tenant_name=tenant_name, users=run_with_timeout(8, list_user_rows, tenant_cfg))
    except Exception as exc:
        flash(f"读取用户列表失败: {exc}", "error")
        return redirect(url_for("tenant.index"))


@tenant_bp.route("/tenant/<tenant_name>/user/create", methods=["POST"])
def create_user(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    is_admin = request.form.get("is_admin") == "on"
    create_password = request.form.get("create_password") == "on"
    if not username or not email:
        flash("用户名和邮箱不能为空。", "error")
        return redirect(url_for("tenant.list_users", tenant_name=tenant_name))
    try:
        for message in create_user_for_tenant(tenant_cfg, username, email, is_admin, create_password):
            flash(message, "success")
    except Exception as exc:
        flash(f"创建用户失败: {exc}", "error")
    return redirect(url_for("tenant.list_users", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/user/reset_mfa/<user_id>", methods=["POST"])
def reset_mfa(tenant_name: str, user_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        reset_user_mfa(tenant_cfg, user_id)
        flash("用户 MFA 已重置。", "success")
    except Exception as exc:
        flash(f"重置 MFA 失败: {exc}", "error")
    return redirect(url_for("tenant.list_users", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instances")
def list_instances(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        return render_page("instances", tenant_name=tenant_name, instances=run_with_timeout(8, list_instance_rows, tenant_cfg))
    except Exception as exc:
        flash(f"读取实例列表失败: {exc}", "error")
        return redirect(url_for("tenant.index"))


@tenant_bp.route("/tenant/<tenant_name>/instance/action/<instance_id>/<action>", methods=["POST"])
def instance_action(tenant_name: str, instance_id: str, action: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    if action not in INSTANCE_ACTIONS:
        flash(f"不支持的实例操作: {action}", "error")
        return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))
    try:
        get_compute_client(tenant_cfg).instance_action(instance_id, INSTANCE_ACTIONS[action])
        flash(f"实例操作已提交: {action}", "success")
    except Exception as exc:
        flash(f"实例操作失败: {exc}", "error")
    if request.form.get("next_page") == "rescue":
        return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))
    return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/change_ip", methods=["GET", "POST"])
def change_ip(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        if request.method == "POST":
            new_ip, _ = replace_public_ip(tenant_cfg, instance_id)
            flash(f"已分配新的公网 IP: {new_ip}", "success")
            return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))
        return render_page("change_ip", tenant_name=tenant_name, instance=change_ip_context(tenant_cfg, instance_id))
    except Exception as exc:
        flash(f"更换公网 IP 失败: {exc}", "error")
        return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/launcher")
def launcher(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        context = run_with_timeout(8, launch_context, tenant_cfg)
        return render_page("launcher", tenant_name=tenant_name, tasks=filtered_tasks(tenant_name), **context)
    except Exception as exc:
        flash(f"读取抢机所需资源失败: {exc}", "error")
        return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/launcher/start", methods=["POST"])
def launcher_start(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    task_id = uuid4().hex[:12]
    form = request.form.to_dict()
    task = {
        "id": task_id,
        "tenant_name": tenant_name,
        "preset": form.get("preset", "amd1c1g"),
        "status": "queued",
        "current_attempt": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "logs": [],
        "cancel_requested": False,
        "display_name": form.get("display_name") or "",
    }
    with TASK_LOCK:
        LAUNCH_TASKS[task_id] = task
    threading.Thread(target=launch_worker, args=(task_id, tenant_cfg, form), daemon=True).start()
    flash(f"抢机任务 {task_id} 已启动。", "success")
    return redirect(url_for("tenant.launcher", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/launcher/task/<task_id>/cancel", methods=["POST"])
def launcher_cancel(tenant_name: str, task_id: str):
    snapshot = task_snapshot(task_id)
    if snapshot and snapshot.get("tenant_name") == tenant_name:
        update_task(task_id, cancel_requested=True)
        append_task_log(task_id, "已收到取消请求，将在下一轮检查时停止。")
        flash(f"已请求取消任务 {task_id}。", "success")
    return redirect(url_for("tenant.launcher", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue")
def rescue_center(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        return render_page("rescue", tenant_name=tenant_name, **run_with_timeout(8, rescue_context, tenant_cfg, instance_id))
    except Exception as exc:
        flash(f"读取救机信息失败: {exc}", "error")
        return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue/console", methods=["POST"])
def create_console_connection(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    public_key = request.form.get("ssh_public_key", "").strip()
    if not public_key.startswith("ssh-"):
        flash("请粘贴有效的 SSH 公钥。", "error")
        return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))
    try:
        connection_id = create_console(tenant_cfg, instance_id, public_key)
        flash(f"串口控制台连接已创建: {connection_id}", "success")
    except Exception as exc:
        flash(f"创建串口控制台连接失败: {exc}", "error")
    return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue/boot-volume", methods=["POST"])
def resize_boot_volume(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        new_size = int(request.form.get("size_in_gbs", "0"))
        if new_size < 50 or new_size > 32768:
            flash("引导卷大小必须在 50 GB 到 32768 GB 之间。", "error")
            return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))
        expand_boot_volume(tenant_cfg, instance_id, new_size)
        flash(f"引导卷扩容请求已提交，新容量 {new_size} GB。", "success")
    except Exception as exc:
        flash(f"引导卷扩容失败: {exc}", "error")
    return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))


@tenant_bp.route("/tenant/<tenant_name>/security-lists")
def list_security_lists(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        return render_page("security_lists", tenant_name=tenant_name, security_lists=run_with_timeout(8, list_security_list_rows, tenant_cfg))
    except Exception as exc:
        flash(f"读取安全列表失败: {exc}", "error")
        return redirect(url_for("tenant.index"))


@tenant_bp.route("/tenant/<tenant_name>/security-list/<security_list_id>/rules")
def security_list_rules(tenant_name: str, security_list_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        return render_page("security_rules", tenant_name=tenant_name, **run_with_timeout(8, security_rules_context, tenant_cfg, security_list_id))
    except Exception as exc:
        flash(f"读取安全规则失败: {exc}", "error")
        return redirect(url_for("tenant.list_security_lists", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/security-list/<security_list_id>/add-rule", methods=["POST"])
def add_rule(tenant_name: str, security_list_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        append_security_rule(
            tenant_cfg,
            security_list_id,
            request.form.get("rule_type", "ingress"),
            request.form.get("protocol", "tcp"),
            request.form.get("source_dest", "0.0.0.0/0").strip(),
            request.form.get("port_min", "").strip(),
            request.form.get("port_max", "").strip(),
            request.form.get("description", "").strip(),
        )
        flash("安全规则已添加。", "success")
    except Exception as exc:
        flash(f"添加规则失败: {exc}", "error")
    return redirect(url_for("tenant.security_list_rules", tenant_name=tenant_name, security_list_id=security_list_id))


@tenant_bp.route("/tenant/<tenant_name>/security-list/<security_list_id>/delete-rule/<rule_type>/<int:rule_index>", methods=["POST"])
def delete_rule(tenant_name: str, security_list_id: str, rule_type: str, rule_index: int):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        remove_security_rule(tenant_cfg, security_list_id, rule_type, rule_index)
        flash("安全规则已删除。", "success")
    except Exception as exc:
        flash(f"删除规则失败: {exc}", "error")
    return redirect(url_for("tenant.security_list_rules", tenant_name=tenant_name, security_list_id=security_list_id))
