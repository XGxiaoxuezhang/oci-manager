from __future__ import annotations

from flask import Blueprint, flash, redirect, request, session, url_for

from email_service import create_dkim, create_domain, create_sender, delete_domain, delete_sender, email_context, generate_smtp_credential, send_test_email
from rendering import render_page
from tenant_routes import require_tenant
from timeout_utils import run_with_timeout

email_bp = Blueprint("email", __name__)


@email_bp.route("/tenant/<tenant_name>/email")
def email_home(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        new_smtp_credential = session.pop("new_smtp_credential", None)
        context = run_with_timeout(8, email_context, tenant_cfg)
        return render_page("email", tenant_name=tenant_name, new_smtp_credential=new_smtp_credential, **context)
    except Exception as exc:
        flash(f"读取邮件配置失败：{exc}", "error")
        return redirect(url_for("tenant.index"))


@email_bp.route("/tenant/<tenant_name>/email/domains", methods=["POST"])
def email_create_domain(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    domain_name = request.form.get("domain_name", "").strip()
    description = request.form.get("description", "").strip()
    if not domain_name:
        flash("请填写发信域名。", "error")
        return redirect(url_for("email.email_home", tenant_name=tenant_name))
    try:
        domain = create_domain(tenant_cfg, domain_name, description)
        flash(f"域名 {domain['name']} 已提交创建请求。", "success")
    except Exception as exc:
        flash(f"创建域名失败：{exc}", "error")
    return redirect(url_for("email.email_home", tenant_name=tenant_name))


@email_bp.route("/tenant/<tenant_name>/email/domains/<domain_id>/delete", methods=["POST"])
def email_delete_domain(tenant_name: str, domain_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        delete_domain(tenant_cfg, domain_id)
        flash("域名删除请求已提交。", "success")
    except Exception as exc:
        flash(f"删除域名失败：{exc}", "error")
    return redirect(url_for("email.email_home", tenant_name=tenant_name))


@email_bp.route("/tenant/<tenant_name>/email/domains/<domain_id>/dkim", methods=["POST"])
def email_create_dkim(tenant_name: str, domain_id: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    name = request.form.get("selector_name", "selector1").strip()
    try:
        create_dkim(tenant_cfg, domain_id, name)
        flash("已提交生成 DKIM 请求，可能需要等待片刻生效。", "success")
    except Exception as exc:
        flash(f"生成 DKIM 失败：{exc}", "error")
    return redirect(url_for("email.email_home", tenant_name=tenant_name))


@email_bp.route("/tenant/<tenant_name>/email/smtp-credential", methods=["POST"])
def email_create_smtp_credential(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        cred = generate_smtp_credential(tenant_cfg)
        session["new_smtp_credential"] = cred
        flash("SMTP 账号生成成功！注意保存密码，以后无法再次查看。", "success")
    except Exception as exc:
        flash(f"生成 SMTP 账号失败：{exc}", "error")
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
            f"测试邮件已提交。message_id={result['message_id'] or '-'} envelope_id={result['envelope_id'] or '-'}{extra}",
            "success",
        )
    except Exception as exc:
        flash(f"发送测试邮件失败：{exc}", "error")
    return redirect(url_for("email.email_home", tenant_name=tenant_name))
