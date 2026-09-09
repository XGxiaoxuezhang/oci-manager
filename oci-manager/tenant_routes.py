from __future__ import annotations

import threading
from uuid import uuid4

from flask import Blueprint, flash, redirect, request, url_for

from audit import list_audit_events
from export_utils import send_csv
from audit_service import RANGE_OPTIONS, cloud_audit_context
from launch_manager import LAUNCH_TASKS, TASK_LOCK, append_task_log, clear_finished_tasks, filtered_tasks, launch_context, launch_worker, save_launch_tasks, task_snapshot, update_task
from rendering import render_page
from settings import DEFAULT_REGIONS, INSTANCE_ACTIONS
from storage import load_tenants, normalize_tenant_name, now_iso, save_tenants
from tenant_services import (
    append_security_rule,
    capture_console_history,
    change_ip_context,
    console_history_content,
    console_private_key_path,
    create_console,
    create_tenant_record,
    create_user_for_tenant,
    dashboard_context,
    delete_api_key,
    expand_boot_volume,
    iam_security_context,
    instance_available_actions,
    instance_edit_context,
    instance_overview_context,
    list_console_history_rows,
    list_instance_rows,
    list_public_ip_rows,
    list_security_list_rows,
    list_user_rows,
    release_public_ip,
    remove_security_rule,
    remove_tenant_record,
    replace_public_ip,
    rescue_context,
    reset_user_mfa,
    rotate_api_key,
    security_rules_context,
    terminate_instance,
    update_instance_configuration,
)
from compartment_service import apply_session_compartment, switch_compartment as apply_compartment_switch
from schedule_service import (
    add_schedule,
    days_label,
    list_schedules,
    remove_schedule,
    toggle_schedule,
)
from network_service import NSG_PROTOCOLS, add_nsg_rule, network_context, nsg_context, nsg_rules_context, remove_nsg_rule, update_nsg_rule
from oci_helpers import find_tenant_config, get_compute_client
from volume_service import (
    attach_volume,
    create_backup,
    create_volume,
    delete_backup,
    delete_boot_volume,
    delete_volume,
    detach_volume,
    resize_boot_volume,
    resize_volume,
    restore_backup,
    volume_context,
)
from timeout_utils import run_with_timeout
from vnc_tunnel import start_tunnel, start_web_vnc, stop_tunnel, stop_web_vnc, web_vnc_status

tenant_bp = Blueprint("tenant", __name__)


def require_tenant(tenant_name: str):
    """取租户配置，并绑定当前选中的 compartment。

    绑定后的 cfg 会一路传到服务层与后台线程（抢实例任务），所以它们不必依赖
    request context 就能拿到正确的 compartment。同时挂到 g 上供模板注入。
    """
    from flask import g

    tenant_cfg = find_tenant_config(tenant_name)
    if tenant_cfg is None:
        flash(f"租户 {tenant_name} 不存在。", "error")
        return None
    tenant_cfg = apply_session_compartment(tenant_cfg, tenant_name)
    g.tenant_cfg = tenant_cfg
    g.tenant_name = tenant_name
    # ?refresh=1 绕过 TTL 缓存强制拉新
    g.cache_bypass = request.args.get("refresh") == "1"
    # 所有写操作都是 POST 且都走这里：请求开始即清该租户缓存，
    # redirect 后的 GET 重新拉云端最新状态
    if request.method == "POST":
        from cache import invalidate_tenant

        invalidate_tenant(tenant_name)
    return tenant_cfg


def looks_like_ocid(value: str, kind: str) -> bool:
    return value.startswith(f"ocid1.{kind}.")


def looks_like_fingerprint(value: str) -> bool:
    parts = value.split(":")
    return len(parts) == 16 and all(len(part) == 2 and all(ch in "0123456789abcdefABCDEF" for ch in part) for part in parts)


def _dashboard_context() -> dict[str, Any]:
    """首页与租户页共用的仪表盘数据。

    原先两个 handler 逐字重复，这里收拢成一份。
    """
    context = dashboard_context(load_tenants())
    context["recent_events"] = list_audit_events(6)
    with TASK_LOCK:
        tasks = [{**task, "logs": list(task.get("logs", []))} for task in LAUNCH_TASKS.values()]
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    context["recent_tasks"] = tasks[:6]
    return context


@tenant_bp.route("/")
def index():
    return render_page("home", **_dashboard_context())


@tenant_bp.route("/tenants")
def tenants_home():
    return render_page("tenants", **_dashboard_context())


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
    context.update(run_with_timeout(30, instance_overview_context, tenants))
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


@tenant_bp.route("/tenant/<tenant_name>/compartment", methods=["POST"])
def set_compartment(tenant_name: str):
    """切换当前租户的 compartment 作用域。

    拿未绑定 compartment 的原始配置去做校验，避免用「上一次的选择」去验证
    「这一次的选择」，导致切到无效 compartment 后无法回到根。
    """
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    apply_compartment_switch(find_tenant_config(tenant_name) or tenant_cfg, tenant_name, request.form.get("compartment_id", ""))
    return redirect(request.form.get("next") or request.referrer or url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/users")
def list_users(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        ctx = run_with_timeout(15, iam_security_context, tenant_cfg)
        return render_page("users", tenant_name=tenant_name, **ctx)
    except Exception as exc:
        flash(f"读取用户列表失败: {exc}", "error")
        return render_page("users", tenant_name=tenant_name, users=[], summary={}, findings=[], load_error=str(exc))


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


@tenant_bp.route("/tenant/<tenant_name>/user/rotate-key", methods=["POST"])
def rotate_key(tenant_name: str):
    """轮换当前配置用户的 API 密钥：先上传云端，成功后再换本地私钥。"""
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    if request.form.get("confirm_text", "").strip().upper() != "ROTATE":
        flash("确认文本不匹配，未轮换密钥。请输入 ROTATE。", "error")
        return redirect(url_for("tenant.list_users", tenant_name=tenant_name))
    try:
        result = rotate_api_key(tenant_cfg)
        flash(f"密钥已轮换，新指纹 {result['fingerprint']}。", "success")
        if result.get("backup_path"):
            flash(f"旧私钥已备份到 {result['backup_path']}。", "success")
        flash(f"旧指纹 {result.get('old_fingerprint') or '-'} 仍保留在 OCI，确认新密钥可用后到下方删除。", "error")
    except Exception as exc:
        flash(f"轮换密钥失败: {exc}", "error")
    return redirect(url_for("tenant.list_users", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/user/<user_id>/delete-key/<fingerprint>", methods=["POST"])
def delete_key(tenant_name: str, user_id: str, fingerprint: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        delete_api_key(tenant_cfg, user_id, fingerprint)
        flash(f"已删除 API Key {fingerprint[:16]}…。", "success")
    except Exception as exc:
        flash(f"删除 API Key 失败: {exc}", "error")
    return redirect(url_for("tenant.list_users", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/instances")
def list_instances(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    schedules = [{**item, "days_label": days_label(item["days"])} for item in list_schedules(tenant_name)]
    context = {
        "tenant_name": tenant_name,
        "instances": [],
        "tasks": filtered_tasks(tenant_name),
        "availability_domains": [],
        "subnets": [],
        "images": [],
        "schedules": schedules,
        "scheduled_instance_ids": {item["instance_id"] for item in schedules if item["enabled"]},
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
        compute_client = get_compute_client(tenant_cfg)
        current_state = compute_client.get_instance(instance_id).data.lifecycle_state
        if action not in instance_available_actions(current_state):
            flash(f"当前实例状态 {current_state} 不允许执行 {action} 操作。", "error")
            return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))
        compute_client.instance_action(instance_id, INSTANCE_ACTIONS[action])
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
            release_old = request.form.get("release_old") == "on"
            new_ip, info = replace_public_ip(tenant_cfg, instance_id, release_old=release_old)
            for released in info.get("released", []):
                flash(f"已释放旧公网 IP {released}。", "success")
            if info.get("release_error"):
                flash(info["release_error"], "error")
            flash(f"已分配新的公网 IP: {new_ip}", "success")
            return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))
        return render_page("change_ip", tenant_name=tenant_name, instance=change_ip_context(tenant_cfg, instance_id))
    except Exception as exc:
        flash(f"更换公网 IP 失败: {exc}", "error")
        return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/public-ips")
def list_public_ips(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        rows = run_with_timeout(20, list_public_ip_rows, tenant_cfg)
        return render_page(
            "public_ips",
            tenant_name=tenant_name,
            public_ips=rows,
            orphan_count=sum(1 for row in rows if not row["attached"]),
        )
    except Exception as exc:
        flash(f"读取公网 IP 失败: {exc}", "error")
        return render_page("public_ips", tenant_name=tenant_name, public_ips=[], orphan_count=0, load_error=str(exc))


@tenant_bp.route("/tenant/<tenant_name>/public-ips/release", methods=["POST"])
def release_public_ip_view(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    public_ip_id = request.form.get("public_ip_id", "").strip()
    try:
        if not public_ip_id:
            raise ValueError("缺少公网 IP 标识。")
        released = release_public_ip(tenant_cfg, public_ip_id)
        flash(f"已释放公网 IP {released}。", "success")
    except Exception as exc:
        flash(f"释放公网 IP 失败: {exc}", "error")
    return redirect(url_for("tenant.list_public_ips", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/network")
def network(tenant_name: str):
    """VCN / 子网 / 路由表 / 网关只读视图。"""
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        context = run_with_timeout(30, network_context, tenant_cfg)
    except Exception as exc:
        context = {"vcns": [], "subnets": [], "route_tables": [], "gateways": [], "load_error": str(exc)}
    return render_page("network", tenant_name=tenant_name, **context)


@tenant_bp.route("/tenant/<tenant_name>/nsg")
def list_nsgs(tenant_name: str):
    """网络安全组列表。NSG 与 Security List 是并集生效，两边都要看。"""
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        context = run_with_timeout(25, nsg_context, tenant_cfg)
    except Exception as exc:
        context = {"nsgs": [], "load_error": str(exc)}
        flash(f"读取网络安全组失败: {exc}", "error")
    return render_page("nsg", tenant_name=tenant_name, **context)


@tenant_bp.route("/tenant/<tenant_name>/nsg/<nsg_id>")
def nsg_rules(tenant_name: str, nsg_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        context = run_with_timeout(25, nsg_rules_context, tenant_cfg, nsg_id)
    except Exception as exc:
        flash(f"读取 NSG 规则失败: {exc}", "error")
        return redirect(url_for("tenant.list_nsgs", tenant_name=tenant_name))
    return render_page("nsg_rules", tenant_name=tenant_name, protocol_options=NSG_PROTOCOLS, **context)


@tenant_bp.route("/tenant/<tenant_name>/nsg/<nsg_id>/rule/add", methods=["POST"])
def add_nsg_rule_view(tenant_name: str, nsg_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        add_nsg_rule(
            tenant_cfg,
            nsg_id,
            request.form.get("direction", "INGRESS"),
            request.form.get("protocol", "6"),
            request.form.get("cidr", ""),
            request.form.get("ports", ""),
            request.form.get("description", ""),
            request.form.get("stateless") == "on",
        )
        flash("NSG 规则已添加。", "success")
    except Exception as exc:
        flash(f"添加 NSG 规则失败: {exc}", "error")
    return redirect(url_for("tenant.nsg_rules", tenant_name=tenant_name, nsg_id=nsg_id))


@tenant_bp.route("/tenant/<tenant_name>/nsg/<nsg_id>/rule/<rule_id>/update", methods=["POST"])
def update_nsg_rule_view(tenant_name: str, nsg_id: str, rule_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        update_nsg_rule(
            tenant_cfg,
            nsg_id,
            rule_id,
            request.form.get("direction", "INGRESS"),
            request.form.get("protocol", "6"),
            request.form.get("cidr", ""),
            request.form.get("ports", ""),
            request.form.get("description", ""),
            request.form.get("stateless") == "on",
        )
        flash("NSG 规则已更新。", "success")
    except Exception as exc:
        flash(f"更新 NSG 规则失败: {exc}", "error")
    return redirect(url_for("tenant.nsg_rules", tenant_name=tenant_name, nsg_id=nsg_id))


@tenant_bp.route("/tenant/<tenant_name>/nsg/<nsg_id>/rule/<rule_id>/delete", methods=["POST"])
def delete_nsg_rule_view(tenant_name: str, nsg_id: str, rule_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        remove_nsg_rule(tenant_cfg, nsg_id, rule_id)
        flash("NSG 规则已删除。", "success")
    except Exception as exc:
        flash(f"删除 NSG 规则失败: {exc}", "error")
    return redirect(url_for("tenant.nsg_rules", tenant_name=tenant_name, nsg_id=nsg_id))


@tenant_bp.route("/tenant/<tenant_name>/volumes")
def volumes(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        context = run_with_timeout(35, volume_context, tenant_cfg)
    except Exception as exc:
        context = {"volumes": [], "boot_volumes": [], "backups": [], "instances": [], "availability_domains": [], "orphan_count": 0, "load_error": str(exc)}
        flash(f"读取块存储失败: {exc}", "error")
    return render_page("volumes", tenant_name=tenant_name, **context)


def _back_to_volumes(tenant_name: str):
    return redirect(url_for("tenant.volumes", tenant_name=tenant_name))


@tenant_bp.route("/tenant/<tenant_name>/volumes/create", methods=["POST"])
def create_volume_view(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        size = int(request.form.get("size_gbs", "50"))
        if size < 1 or size > 32768:
            raise ValueError("卷大小需在 1 ~ 32768 GB 之间。")
        create_volume(tenant_cfg, request.form.get("availability_domain", ""), size, request.form.get("display_name", ""))
        flash("块存储卷已创建。", "success")
    except Exception as exc:
        flash(f"创建卷失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/volume/<volume_id>/attach", methods=["POST"])
def attach_volume_view(tenant_name: str, volume_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        instance_id = request.form.get("instance_id", "").strip()
        if not instance_id:
            raise ValueError("请选择要挂载到的实例。")
        attach_volume(tenant_cfg, volume_id, instance_id)
        flash("已发起挂载，稍后刷新查看状态。", "success")
    except Exception as exc:
        flash(f"挂载失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/volume-attachment/<attachment_id>/detach", methods=["POST"])
def detach_volume_view(tenant_name: str, attachment_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        detach_volume(tenant_cfg, attachment_id)
        flash("已发起卸载，稍后刷新查看状态。", "success")
    except Exception as exc:
        flash(f"卸载失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/volume/<volume_id>/resize", methods=["POST"])
def resize_volume_view(tenant_name: str, volume_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        size = int(request.form.get("size_gbs", "0"))
        if size < 1 or size > 32768:
            raise ValueError("卷大小需在 1 ~ 32768 GB 之间。")
        if size <= int(request.form.get("current_size", "0") or 0):
            raise ValueError("新大小必须大于当前大小，OCI 卷只能扩容不能缩容。")
        resize_volume(tenant_cfg, volume_id, size)
        flash(f"已提交扩容到 {size} GB。", "success")
    except Exception as exc:
        flash(f"扩容失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/volume/<volume_id>/delete", methods=["POST"])
def delete_volume_view(tenant_name: str, volume_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        if request.form.get("confirm_name", "").strip() != request.form.get("volume_name", "").strip():
            raise ValueError("名称校验未通过，已取消删除。")
        delete_volume(tenant_cfg, volume_id)
        flash("卷已删除。", "success")
    except Exception as exc:
        flash(f"删除卷失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/volume/<volume_id>/backup", methods=["POST"])
def backup_volume_view(tenant_name: str, volume_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        create_backup(tenant_cfg, volume_id, request.form.get("display_name", ""))
        flash("已发起备份，可在下方备份列表查看进度。", "success")
    except Exception as exc:
        flash(f"创建备份失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/boot-volume/<boot_volume_id>/resize", methods=["POST"])
def resize_boot_volume_view(tenant_name: str, boot_volume_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        size = int(request.form.get("size_gbs", "0"))
        if size <= int(request.form.get("current_size", "0") or 0):
            raise ValueError("新大小必须大于当前大小，引导卷只能扩容不能缩容。")
        resize_boot_volume(tenant_cfg, boot_volume_id, size)
        flash(f"已提交引导卷扩容到 {size} GB。", "success")
    except Exception as exc:
        flash(f"引导卷扩容失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/boot-volume/<boot_volume_id>/delete", methods=["POST"])
def delete_boot_volume_view(tenant_name: str, boot_volume_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        if request.form.get("confirm_name", "").strip() != request.form.get("volume_name", "").strip():
            raise ValueError("名称校验未通过，已取消删除。")
        delete_boot_volume(tenant_cfg, boot_volume_id)
        flash("引导卷已删除。", "success")
    except Exception as exc:
        flash(f"删除引导卷失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/volume-backup/<backup_id>/restore", methods=["POST"])
def restore_backup_view(tenant_name: str, backup_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        volume_id = restore_backup(
            tenant_cfg,
            backup_id,
            request.form.get("availability_domain", ""),
            request.form.get("display_name", ""),
        )
        flash(f"已从备份创建新卷 {volume_id[:24]}…，请在卷列表查看。", "success")
    except Exception as exc:
        flash(f"从备份恢复失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/volume-backup/<backup_id>/delete", methods=["POST"])
def delete_backup_view(tenant_name: str, backup_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        delete_backup(tenant_cfg, backup_id)
        flash("备份已删除。", "success")
    except Exception as exc:
        flash(f"删除备份失败: {exc}", "error")
    return _back_to_volumes(tenant_name)


@tenant_bp.route("/tenant/<tenant_name>/cloud-audit")
def cloud_audit(tenant_name: str):
    """OCI 云端审计日志：谁在什么时候、用什么凭据、从哪个 IP 动过什么。"""
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        hours = int(request.args.get("hours", "24"))
    except ValueError:
        hours = 24
    keyword = request.args.get("q", "").strip()
    context = {
        "tenant_name": tenant_name,
        "audit_events": [],
        "audit_summary": {"total": 0, "top_principals": [], "top_ips": [], "top_actions": []},
        "audit_hours": hours,
        "audit_keyword": keyword,
        "audit_range_options": RANGE_OPTIONS,
    }
    try:
        context.update(run_with_timeout(40, cloud_audit_context, tenant_cfg, hours, keyword))
        context["tenant_name"] = tenant_name
    except Exception as exc:
        flash(f"读取云端审计日志失败: {exc}", "error")
        context["load_error"] = str(exc)
    return render_page("cloud_audit", **context)


@tenant_bp.route("/tenant/<tenant_name>/instance/<instance_id>/edit", methods=["GET", "POST"])
def edit_instance(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    if request.method == "POST":
        try:
            result = update_instance_configuration(
                tenant_cfg,
                instance_id,
                request.form.get("display_name", ""),
                request.form.get("ocpus", ""),
                request.form.get("memory_gbs", ""),
            )
            changed = "、".join("显示名称" if item == "name" else "CPU/内存" for item in result["changed"])
            flash(f"实例 {result['instance_name']} 更新请求已提交：{changed}。", "success")
            return redirect(url_for("tenant.list_instances", tenant_name=tenant_name))
        except Exception as exc:
            flash(f"更新实例失败: {exc}", "error")
    try:
        context = instance_edit_context(tenant_cfg, instance_id)
        return render_page("instance_edit", tenant_name=tenant_name, **context)
    except Exception as exc:
        flash(f"读取实例编辑信息失败: {exc}", "error")
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


# ===== 定时启停 =====


@tenant_bp.route("/tenant/<tenant_name>/schedules/add", methods=["POST"])
def schedule_add(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    instance_id = request.form.get("instance_id", "").strip()
    instance_name = request.form.get("instance_name", "").strip() or instance_id
    action = request.form.get("action", "stop").strip()
    time_hhmm = request.form.get("time", "").strip()
    days = request.form.get("days", "all").strip()
    try:
        add_schedule(tenant_name, instance_id, instance_name, action, time_hhmm, days)
        flash(f"已添加定时计划：{instance_name} 每天 {time_hhmm} {('停止' if action == 'stop' else '启动')}。", "success")
    except Exception as exc:
        flash(f"添加定时计划失败: {exc}", "error")
    return redirect(url_for("tenant.list_instances", tenant_name=tenant_name) + "#schedules")


@tenant_bp.route("/tenant/<tenant_name>/schedules/<int:schedule_id>/delete", methods=["POST"])
def schedule_delete(tenant_name: str, schedule_id: int):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    remove_schedule(tenant_name, schedule_id)
    flash("定时计划已删除。", "success")
    return redirect(url_for("tenant.list_instances", tenant_name=tenant_name) + "#schedules")


@tenant_bp.route("/tenant/<tenant_name>/schedules/<int:schedule_id>/toggle", methods=["POST"])
def schedule_toggle(tenant_name: str, schedule_id: int):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    schedules = {item["id"]: item for item in list_schedules(tenant_name)}
    item = schedules.get(schedule_id)
    if item is None:
        flash("定时计划不存在。", "error")
    else:
        toggle_schedule(tenant_name, schedule_id, not item["enabled"])
        flash("定时计划已{}。".format("启用" if not item["enabled"] else "停用"), "success")
    return redirect(url_for("tenant.list_instances", tenant_name=tenant_name) + "#schedules")


# ===== 控制台历史（启动日志）=====


@tenant_bp.route("/tenant/<tenant_name>/console-history")
def console_history(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    history_id = request.args.get("history_id", "").strip()
    rows: list[dict] = []
    content = ""
    state = ""
    pending = False
    try:
        rows = run_with_timeout(15, list_console_history_rows, tenant_cfg)
        if history_id:
            state, content = run_with_timeout(15, console_history_content, tenant_cfg, history_id)
            pending = state not in ("SUCCEEDED", "FAILED")
            if pending:
                # 生成中：页面 5 秒自动刷新等结果
                pass
    except Exception as exc:
        flash(f"读取控制台历史失败: {exc}", "error")
    return render_page(
        "console_history",
        tenant_name=tenant_name,
        rows=rows,
        history_id=history_id,
        content=content,
        state=state,
        pending=pending,
    )


@tenant_bp.route("/tenant/<tenant_name>/console-history/capture/<instance_id>", methods=["POST"])
def console_history_capture(tenant_name: str, instance_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        result = run_with_timeout(15, capture_console_history, tenant_cfg, instance_id)
        flash(f"已发起抓取（状态 {result['state']}），稍候查看内容。", "success")
        return redirect(url_for("tenant.console_history", tenant_name=tenant_name, history_id=result["id"]))
    except Exception as exc:
        flash(f"抓取控制台历史失败: {exc}", "error")
        return redirect(url_for("tenant.console_history", tenant_name=tenant_name))
