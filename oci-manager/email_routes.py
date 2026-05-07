from __future__ import annotations

from flask import Blueprint, flash, redirect, request, url_for

from email_service import create_email_domain, create_sender, delete_sender, email_context, send_test_email
from export_utils import send_csv
from oci_helpers import build_dashboard_cards
from rendering import render_page
from storage import load_tenants
from tenant_routes import require_tenant
from timeout_utils import run_with_timeout

email_bp = Blueprint("email", __name__)


@email_bp.route("/email")
def email_overview():
    tenants = load_tenants()
    if tenants:
        return redirect(url_for("email.email_home", tenant_name=sorted(tenants.keys())[0]))
    return render_page("email_overview", module_title="邮件", module_path="email", tenants=build_dashboard_cards(tenants))


@email_bp.route("/tenant/<tenant_name>/email")
def email_home(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        context = run_with_timeout(8, email_context, tenant_cfg)
        return render_page("email", tenant_name=tenant_name, **context)
    except Exception as exc:
        flash(f"读取邮件配置失败：{exc}", "error")
        return render_page(
            "email",
            tenant_name=tenant_name,
            email_stats={"domain_count": 0, "active_domain_count": 0, "sender_count": 0},
            email_guides=[],
            email_configuration={"http_submit_endpoint": "-", "smtp_submit_endpoint": "-"},
            email_domains=[],
            dkims=[],
            senders=[],
            load_error=str(exc),
        )


@email_bp.route("/tenant/<tenant_name>/email/export")
def email_export(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    export_type = request.args.get("type", "senders")
    try:
        context = run_with_timeout(8, email_context, tenant_cfg)
        if export_type == "domains":
            return send_csv(f"{tenant_name}-email-domains.csv", ["name", "state", "active_dkim_id", "description", "created", "id"], context["email_domains"])
        if export_type == "dkims":
            return send_csv(f"{tenant_name}-email-dkims.csv", ["domain", "name", "state", "cname_record_value", "txt_record_value", "created", "id"], context["dkims"])
        return send_csv(f"{tenant_name}-email-senders.csv", ["email_address", "state", "email_ip_pool_id", "created", "id"], context["senders"])
    except Exception as exc:
        flash(f"导出邮件配置失败：{exc}", "error")
        return redirect(url_for("email.email_home", tenant_name=tenant_name))


@email_bp.route("/tenant/<tenant_name>/email/domains", methods=["POST"])
def email_create_domain(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        domain = create_email_domain(tenant_cfg, request.form.get("domain_name", ""), request.form.get("description", ""))
        flash(f"邮件域名 {domain['name']} 已提交创建请求。", "success")
    except Exception as exc:
        flash(f"创建邮件域名失败：{exc}", "error")
    return redirect(url_for("email.email_home", tenant_name=tenant_name))


@email_bp.route("/tenant/<tenant_name>/email/senders", methods=["POST"])
def email_create_sender(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    email_address = request.form.get("email_address", "").strip()
    if not email_address:
        flash("请填写发件人邮箱。", "error")
        return redirect(url_for("email.email_home", tenant_name=tenant_name))
    try:
        sender = create_sender(tenant_cfg, email_address)
        flash(f"发件人 {sender['email_address']} 已提交创建请求。", "success")
    except Exception as exc:
        flash(f"创建发件人失败：{exc}", "error")
    return redirect(url_for("email.email_home", tenant_name=tenant_name))


@email_bp.route("/tenant/<tenant_name>/email/senders/<sender_id>/delete", methods=["POST"])
def email_delete_sender(tenant_name: str, sender_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        delete_sender(tenant_cfg, sender_id)
        flash("发件人删除请求已提交。", "success")
    except Exception as exc:
        flash(f"删除发件人失败：{exc}", "error")
    return redirect(url_for("email.email_home", tenant_name=tenant_name))


@email_bp.route("/tenant/<tenant_name>/email/send-test", methods=["POST"])
def email_send_test(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    sender_email = request.form.get("sender_email", "").strip()
    to_email = request.form.get("to_email", "").strip()
    subject = request.form.get("subject", "").strip()
    body_text = request.form.get("body_text", "").strip()
    reply_to = request.form.get("reply_to", "").strip()
    if not sender_email or not to_email:
        flash("发件人邮箱和收件人邮箱不能为空。", "error")
        return redirect(url_for("email.email_home", tenant_name=tenant_name))
    try:
        result = run_with_timeout(
            15,
            send_test_email,
            tenant_cfg,
            sender_email,
            to_email,
            subject,
            body_text,
            reply_to,
        )
        extra = ""
        if result["suppressed_recipients"]:
            extra = f" 抑制收件人：{', '.join(result['suppressed_recipients'])}"
        flash(
            f"测试邮件已提交。消息 ID：{result['message_id'] or '-'}；投递 ID：{result['envelope_id'] or '-'}{extra}",
            "success",
        )
    except Exception as exc:
        flash(f"发送测试邮件失败：{exc}", "error")
    return redirect(url_for("email.email_home", tenant_name=tenant_name))
