from __future__ import annotations

from flask import Blueprint, request

from audit import list_audit_events
from export_utils import send_csv
from rendering import render_page
from storage import fmt_dt

# 审计页展示用：把开发态 endpoint 名映射成中文动作；未收录的原样展示
ACTION_LABELS = {
    "auth.change_password": "修改登录密码",
    "auth.logout": "退出登录",
    "auth.totp_login": "两步验证",
    "backup.backup_restore": "恢复备份",
    "check.checks_run": "运行巡检",
    "database.databases_create": "创建自治数据库",
    "database.databases_wallet": "下载 Wallet",
    "database.databases_start": "启动数据库",
    "database.databases_stop": "停止数据库",
    "database.databases_backup": "数据库手动备份",
    "database.databases_delete": "删除数据库",
    "email.email_create_domain": "创建邮件域名",
    "email.email_create_sender": "创建发件人",
    "email.email_delete_sender": "删除发件人",
    "email.email_send_test": "发送测试邮件",
    "object_storage.object_storage_create_bucket": "创建 Bucket",
    "object_storage.object_storage_delete_bucket": "删除 Bucket",
    "object_storage.object_storage_bucket_settings": "更新 Bucket 设置",
    "object_storage.object_storage_upload": "上传对象",
    "object_storage.object_storage_create_folder": "新建目录",
    "object_storage.object_storage_delete_object": "删除对象",
    "settings_page.notification_save": "保存通知设置",
    "settings_page.notification_test": "发送测试通知",
    "tenant.add_tenant": "添加租户",
    "tenant.delete_tenant": "删除租户",
    "tenant.set_compartment": "切换 Compartment",
    "tenant.create_user": "创建用户",
    "tenant.reset_mfa": "重置 MFA",
    "tenant.rotate_key": "轮换 API Key",
    "tenant.delete_key": "删除 API Key",
    "tenant.instance_action": "实例电源操作",
    "tenant.instance_terminate": "终止实例",
    "tenant.change_ip": "更换公网 IP",
    "tenant.release_public_ip_view": "释放公网 IP",
    "tenant.add_nsg_rule_view": "添加 NSG 规则",
    "tenant.update_nsg_rule_view": "更新 NSG 规则",
    "tenant.delete_nsg_rule_view": "删除 NSG 规则",
    "tenant.create_volume_view": "创建卷",
    "tenant.attach_volume_view": "挂载卷",
    "tenant.detach_volume_view": "卸载卷",
    "tenant.resize_volume_view": "扩容卷",
    "tenant.delete_volume_view": "删除卷",
    "tenant.backup_volume_view": "创建卷备份",
    "tenant.resize_boot_volume_view": "引导卷扩容",
    "tenant.resize_boot_volume": "引导卷扩容",
    "tenant.delete_boot_volume_view": "删除引导卷",
    "tenant.restore_backup_view": "恢复卷备份",
    "tenant.delete_backup_view": "删除卷备份",
    "tenant.edit_instance": "编辑实例",
    "tenant.instance_create_task_start": "发起实例创建任务",
    "tenant.instance_create_task_cancel": "取消创建任务",
    "tenant.instance_tasks_clear": "清理已结束任务",
    "tenant.create_console_connection": "创建串口控制台连接",
    "tenant.start_vnc_tunnel": "启动 VNC 隧道",
    "tenant.start_novnc": "打开 Web VNC",
    "tenant.stop_vnc_tunnel": "停止 VNC 隧道",
    "tenant.add_rule": "添加安全列表规则",
    "tenant.delete_rule": "删除安全列表规则",
    "tenant.schedule_add": "添加定时计划",
    "tenant.schedule_delete": "删除定时计划",
    "tenant.schedule_toggle": "启停定时计划",
    "tenant.console_history_capture": "抓取控制台日志",
}

audit_bp = Blueprint("audit", __name__)


@audit_bp.route("/audit")
def audit_home():
    events = list_audit_events()
    tenant = request.args.get("tenant", "").strip()
    keyword = request.args.get("action", "").strip().lower()
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    tenants = sorted({e.get("tenant_name", "") for e in events} - {""})
    for event in events:
        raw_action = event.get("action", "")
        event["time_text"] = fmt_dt(event.get("time"))
        event["action_label"] = ACTION_LABELS.get(raw_action, raw_action)
    if tenant:
        events = [e for e in events if e.get("tenant_name") == tenant]
    if keyword:
        events = [e for e in events if keyword in e.get("action_label", "").lower() or keyword in e.get("action", "").lower()]
    if date_from:
        events = [e for e in events if (e.get("time") or "") >= date_from]
    if date_to:
        events = [e for e in events if (e.get("time") or "")[:10] <= date_to]
    return render_page("audit", events=events, filter_tenant=tenant, filter_action=request.args.get("action", ""), filter_from=date_from, filter_to=date_to, filter_tenants=tenants)


@audit_bp.route("/audit/export")
def audit_export():
    events = list_audit_events(500)
    rows = [
        {
            "时间": fmt_dt(event.get("time")),
            "用户": event.get("user") or "-",
            "租户": event.get("tenant_name") or "-",
            "动作": ACTION_LABELS.get(event.get("action", ""), event.get("action", "")),
            "状态": event.get("status", ""),
            "路径": (event.get("details") or {}).get("path", ""),
        }
        for event in events
    ]
    headers = ["时间", "用户", "租户", "动作", "状态", "路径"]  # noqa: 导出保持全量最近 500 条
    return send_csv("oci-manager-audit.csv", headers, rows)
