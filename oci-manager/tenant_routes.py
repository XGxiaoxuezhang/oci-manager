from __future__ import annotations

import threading
from uuid import uuid4

from flask import Blueprint, flash, redirect, request, url_for

from audit import list_audit_events
from export_utils import send_csv
from launch_manager import LAUNCH_TASKS, TASK_LOCK, append_task_log, clear_finished_tasks, filtered_tasks, launch_context, launch_worker, save_launch_tasks, task_snapshot, update_task
from rendering import render_page
from settings import DEFAULT_REGIONS, INSTANCE_ACTIONS
from storage import load_tenants, normalize_tenant_name, now_iso, save_tenants
from tenant_services import (
    append_security_rule,
    change_ip_context,
    console_private_key_path,
    create_console,
    create_tenant_record,
    create_user_for_tenant,
    dashboard_context,
    expand_boot_volume,
    instance_overview_context,
    list_instance_rows,
    list_security_list_rows,
    list_user_rows,
    remove_security_rule,
    remove_tenant_record,
    replace_public_ip,
    rescue_context,
    reset_user_mfa,
    security_rules_context,
    terminate_instance,
)
from oci_helpers import find_tenant_config, get_compute_client
from timeout_utils import run_with_timeout
from vnc_tunnel import start_tunnel, start_web_vnc, stop_tunnel, stop_web_vnc, web_vnc_status

tenant_bp = Blueprint("tenant", __name__)


def require_tenant(tenant_name: str):
    tenant_cfg = find_tenant_config(tenant_name)
    if tenant_cfg is None:
        flash(f"租户 {tenant_name} 不存在。", "error")
        return None
    return tenant_cfg


def looks_like_ocid(value: str, kind: str) -> bool:
    return value.startswith(f"ocid1.{kind}.")


def looks_like_fingerprint(value: str) -> bool:
    parts = value.split(":")
    return len(parts) == 16 and all(len(part) == 2 and all(ch in "0123456789abcdefABCDEF" for ch in part) for part in parts)


@tenant_bp.route("/")
def index():
    context = dashboard_context(load_tenants())
    context["recent_events"] = list_audit_events(6)
    with TASK_LOCK:
        tasks = [{**task, "logs": list(task.get("logs", []))} for task in LAUNCH_TASKS.values()]
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    context["recent_tasks"] = tasks[:6]
    return render_page("home", **context)


@tenant_bp.route("/tenants")
def tenants_home():
    context = dashboard_context(load_tenants())
    context["recent_events"] = list_audit_events(6)
    with TASK_LOCK:
        tasks = [{**task, "logs": list(task.get("logs", []))} for task in LAUNCH_TASKS.values()]
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    context["recent_tasks"] = tasks[:6]
    return render_page("tenants", **context)


@tenant_bp.route("/instances")
@tenant_bp.route("/overview/instances")
def instance_overview():
    tenants = load_tenants()
    selected_tenant = request.args.get("tenant", "").strip()
    if not selected_tenant and tenants:
        selected_tenant = sorted(tenants.keys())[0]
    if selected_tenant:
        tenants = {selected_tenant: tenants[selected_tenant]} if selected_tenant in tenants else {}
    context = dashboard_context(tenants)
    context["selected_tenant"] = selected_tenant
    context.update(run_with_timeout(20, instance_overview_context, tenants))
    return render_page("instance_overview", **context)


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
        if not looks_like_ocid(tenant_id, "tenancy") or not looks_like_ocid(user_id, "user"):
            flash("Tenancy OCID 或 User OCID 格式不正确。", "error")
            return redirect(url_for("tenant.add_tenant"))
        if not region:
            flash("区域不能为空。", "error")
            return redirect(url_for("tenant.add_tenant"))
        if not looks_like_fingerprint(fingerprint):
            flash("Fingerprint 格式不正确。", "error")
            return redirect(url_for("tenant.add_tenant"))
        if not key_file or not key_file.filename:
            flash("请上传 OCI API 私钥文件。", "error")
            return redirect(url_for("tenant.add_tenant"))
        if not key_file.filename.lower().endswith(".pem"):
            flash("私钥文件必须是 .pem。", "error")
            return redirect(url_for("tenant.add_tenant"))

        tenants = load_tenants()
        if tenant_name in tenants:
            flash(f"租户 {tenant_name} 已存在，请换一个名称。", "error")
            return redirect(url_for("tenant.add_tenant"))

        try:
            create_tenant_record(tenants, tenant_name, tenant_id, user_id, region, fingerprint, key_file)
        except Exception as exc:
            flash(f"保存租户失败: {exc}", "error")
            return redirect(url_for("tenant.add_tenant"))
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
    if request.form.get("confirm_name", "").strip() != tenant_name:
        flash("确认名称不匹配，未删除租户。", "error")
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
        return render_page("users", tenant_name=tenant_name, users=[], load_error=str(exc))


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
    context = {
        "tenant_name": tenant_name,
        "instances": [],
        "tasks": filtered_tasks(tenant_name),
        "availability_domains": [],
        "subnets": [],
        "images": [],
    }
    try:
        context["instances"] = run_with_timeout(45, list_instance_rows, tenant_cfg)
    except Exception as exc:
        flash(f"读取实例列表失败: {exc}", "error")
        context["load_error"] = str(exc)
    try:
        context.update(run_with_timeout(20, launch_context, tenant_cfg))
    except Exception as exc:
        context["launch_error"] = str(exc)
    return render_page("instances", **context)


@tenant_bp.route("/tenant/<tenant_name>/instances/export")
def export_instances(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        rows = run_with_timeout(45, list_instance_rows, tenant_cfg)
        return send_csv(
            f"{tenant_name}-instances.csv",
            ["name", "shape", "state", "availability_domain", "public_ip", "private_ip", "ipv6", "created", "id"],
            rows,
        )
    except Exception as exc:
        flash(f"导出实例失败: {exc}", "error")
        return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


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


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/terminate", methods=["POST"])
def instance_terminate(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        name = terminate_instance(
            tenant_cfg,
            instance_id,
            request.form.get("confirm_name", ""),
            request.form.get("preserve_boot_volume") == "on",
        )
        flash(f"实例 {name} 终止请求已提交。", "success")
    except Exception as exc:
        flash(f"终止实例失败: {exc}", "error")
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
    return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instances/create", methods=["POST"])
@tenant_bp.route("/tenant/<tenant_name>/launcher/start", methods=["POST"])
def instance_create_task_start(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    task_id = uuid4().hex[:12]
    form = request.form.to_dict()
    required_fields = {
        "availability_domain": "可用域",
        "subnet_id": "子网",
        "image_id": "镜像",
    }
    missing = [label for key, label in required_fields.items() if not (form.get(key) or "").strip()]
    if missing:
        flash("缺少创建参数：" + "、".join(missing), "error")
        return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))
    for numeric_field, label in {"ocpus": "OCPU", "memory_gbs": "内存", "boot_volume_gbs": "启动盘"}.items():
        raw = (form.get(numeric_field) or "").strip()
        if raw:
            try:
                value = float(raw)
            except ValueError:
                flash(f"{label} 必须是数字。", "error")
                return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))
            if value <= 0:
                flash(f"{label} 必须大于 0。", "error")
                return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))
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
    save_launch_tasks()
    threading.Thread(target=launch_worker, args=(task_id, tenant_cfg, form), daemon=True).start()
    flash(f"创建任务 {task_id} 已启动。", "success")
    return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instances/task/<task_id>/cancel", methods=["POST"])
@tenant_bp.route("/tenant/<tenant_name>/launcher/task/<task_id>/cancel", methods=["POST"])
def instance_create_task_cancel(tenant_name: str, task_id: str):
    snapshot = task_snapshot(task_id)
    if snapshot and snapshot.get("tenant_name") == tenant_name:
        update_task(task_id, cancel_requested=True)
        append_task_log(task_id, "已收到取消请求，将在下一轮检查时停止。")
        flash(f"已请求取消任务 {task_id}。", "success")
    return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instances/tasks/clear", methods=["POST"])
def instance_tasks_clear(tenant_name: str):
    removed = clear_finished_tasks(tenant_name)
    flash(f"已清理 {removed} 个已结束任务。", "success")
    return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue")
def rescue_center(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        return render_page(
            "rescue",
            tenant_name=tenant_name,
            console_private_key_path=console_private_key_path(),
            **run_with_timeout(30, rescue_context, tenant_cfg, instance_id),
        )
    except Exception as exc:
        flash(f"读取救机信息失败: {exc}", "error")
        return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue/console", methods=["POST"])
def create_console_connection(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    public_key = request.form.get("ssh_public_key", "").strip()
    if public_key and not public_key.startswith("ssh-"):
        flash("请粘贴有效的 SSH 公钥。", "error")
        return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))
    try:
        connection_id, generated = create_console(tenant_cfg, instance_id, public_key)
        suffix = " 已使用本地控制台专用密钥。" if generated else ""
        flash(f"控制台连接已创建: {connection_id}.{suffix}", "success")
    except Exception as exc:
        flash(f"创建串口控制台连接失败: {exc}", "error")
    return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue/vnc/start", methods=["POST"])
def start_vnc_tunnel(tenant_name: str, instance_id: str):
    if require_tenant(tenant_name) is None:
        return redirect(url_for("tenant.index"))
    connection_id = request.form.get("connection_id", "").strip()
    vnc_connection_string = request.form.get("vnc_connection_string", "").strip()
    if not connection_id or not vnc_connection_string:
        flash("没有可用的 VNC 连接串。", "error")
        return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))
    try:
        result = start_tunnel(f"{tenant_name}:{instance_id}:{connection_id}", vnc_connection_string)
        label = "已存在" if result["already_running"] else "已启动"
        flash(f"本地 VNC 隧道{label}: 127.0.0.1:{result['local_port']}。", "success")
    except Exception as exc:
        flash(f"启动本地 VNC 隧道失败: {exc}", "error")
    return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue/novnc/start", methods=["POST"])
def start_novnc(tenant_name: str, instance_id: str):
    if require_tenant(tenant_name) is None:
        return redirect(url_for("tenant.index"))
    connection_id = request.form.get("connection_id", "").strip()
    vnc_connection_string = request.form.get("vnc_connection_string", "").strip()
    if not connection_id or not vnc_connection_string:
        flash("没有可用的 VNC 连接串。", "error")
        return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))
    try:
        result = start_web_vnc(f"{tenant_name}:{instance_id}:{connection_id}", vnc_connection_string)
        flash(f"Web VNC 已启动: WebSocket {result['web_port']} / VNC {result['vnc_port']}。", "success")
        return redirect(url_for("tenant.novnc_view", tenant_name=tenant_name, instance_id=instance_id, connection_id=connection_id))
    except Exception as exc:
        flash(f"启动 Web VNC 失败: {exc}", "error")
        return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue/novnc/<connection_id>")
def novnc_view(tenant_name: str, instance_id: str, connection_id: str):
    if require_tenant(tenant_name) is None:
        return redirect(url_for("tenant.index"))
    status = web_vnc_status(f"{tenant_name}:{instance_id}:{connection_id}")
    if not status.get("running"):
        flash("Web VNC 未运行，请先启动。", "error")
        return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))
    return render_page(
        "novnc",
        tenant_name=tenant_name,
        instance_id=instance_id,
        connection_id=connection_id,
        web_port=status["web_port"],
        vnc_port=status["vnc_port"],
    )


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue/vnc/stop", methods=["POST"])
def stop_vnc_tunnel(tenant_name: str, instance_id: str):
    if require_tenant(tenant_name) is None:
        return redirect(url_for("tenant.index"))
    connection_id = request.form.get("connection_id", "").strip()
    key = f"{tenant_name}:{instance_id}:{connection_id}"
    stopped_web = stop_web_vnc(key)
    stopped = stop_tunnel(key)
    flash("VNC 隧道已停止。" if stopped or stopped_web else "VNC 隧道未运行。", "success")
    return redirect(url_for("tenant.rescue_center", tenant_name=tenant_name, instance_id=instance_id))


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/rescue/boot-volume", methods=["POST"])
def resize_boot_volume(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        new_size = int(request.form.get("size_in_gbs", "0"))
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
        return render_page("security_lists", tenant_name=tenant_name, security_lists=[], load_error=str(exc))


@tenant_bp.route("/tenant/<tenant_name>/security-list/<security_list_id>/rules")
def security_list_rules(tenant_name: str, security_list_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        return render_page("security_rules", tenant_name=tenant_name, **run_with_timeout(8, security_rules_context, tenant_cfg, security_list_id))
    except Exception as exc:
        flash(f"读取安全规则失败: {exc}", "error")
        return render_page(
            "security_rules",
            tenant_name=tenant_name,
            security_list={"id": security_list_id, "name": security_list_id},
            ingress_rules=[],
            egress_rules=[],
            load_error=str(exc),
        )


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
