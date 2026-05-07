from __future__ import annotations

import html
import re
from email.utils import parseaddr
from typing import Any

import oci

from oci_helpers import build_config, client_kwargs, list_all
from storage import fmt_dt

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def get_email_client(tenant_cfg: dict[str, Any]) -> oci.email.EmailClient:
    return oci.email.EmailClient(build_config(tenant_cfg), **client_kwargs())


def get_email_dp_client(tenant_cfg: dict[str, Any]) -> oci.email_data_plane.EmailDPClient:
    return oci.email_data_plane.EmailDPClient(build_config(tenant_cfg), **client_kwargs())


def validate_email_address(value: str, field_name: str = "邮箱") -> str:
    address = parseaddr(value.strip())[1]
    if not address or not EMAIL_RE.match(address):
        raise ValueError(f"{field_name}格式不正确。")
    return address


def _domain_row(domain: Any) -> dict[str, Any]:
    return {
        "id": getattr(domain, "id", ""),
        "name": getattr(domain, "name", ""),
        "state": getattr(domain, "lifecycle_state", "-"),
        "active_dkim_id": getattr(domain, "active_dkim_id", "") or "-",
        "description": getattr(domain, "description", "") or "",
        "created": fmt_dt(getattr(domain, "time_created", None)),
    }


def _dkim_row(domain_name: str, dkim: Any) -> dict[str, Any]:
    return {
        "domain": domain_name,
        "name": getattr(dkim, "name", ""),
        "id": getattr(dkim, "id", ""),
        "state": getattr(dkim, "lifecycle_state", "-"),
        "cname_record_value": getattr(dkim, "cname_record_value", "") or "-",
        "txt_record_value": getattr(dkim, "txt_record_value", "") or "-",
        "created": fmt_dt(getattr(dkim, "time_created", None)),
    }


def _sender_row(sender: Any) -> dict[str, Any]:
    return {
        "id": getattr(sender, "id", ""),
        "email_address": getattr(sender, "email_address", ""),
        "state": getattr(sender, "lifecycle_state", "-"),
        "email_ip_pool_id": getattr(sender, "email_ip_pool_id", "") or "-",
        "created": fmt_dt(getattr(sender, "time_created", None)),
    }


def email_context(tenant_cfg: dict[str, Any]) -> dict[str, Any]:
    client = get_email_client(tenant_cfg)
    compartment_id = tenant_cfg["tenant_id"]
    configuration = client.get_email_configuration(compartment_id=compartment_id).data
    domains = list_all(client.list_email_domains, compartment_id=compartment_id)
    senders = list_all(client.list_senders, compartment_id=compartment_id)

    domain_rows = sorted((_domain_row(item) for item in domains), key=lambda row: row["name"].lower())
    sender_rows = sorted((_sender_row(item) for item in senders), key=lambda row: row["email_address"].lower())
    dkim_rows: list[dict[str, Any]] = []
    for domain in domains:
        domain_id = getattr(domain, "id", "")
        if not domain_id:
            continue
        try:
            dkim_rows.extend(_dkim_row(getattr(domain, "name", ""), item) for item in list_all(client.list_dkims, email_domain_id=domain_id))
        except Exception:
            continue

    return {
        "email_configuration": {
            "http_submit_endpoint": getattr(configuration, "http_submit_endpoint", "") or "-",
            "smtp_submit_endpoint": getattr(configuration, "smtp_submit_endpoint", "") or "-",
        },
        "email_domains": domain_rows,
        "dkims": sorted(dkim_rows, key=lambda row: (row["domain"].lower(), row["name"].lower())),
        "senders": sender_rows,
        "email_stats": {
            "domain_count": len(domain_rows),
            "active_domain_count": sum(1 for item in domain_rows if item["state"] == "ACTIVE"),
            "sender_count": len(sender_rows),
        },
        "email_guides": [
            "OCI Email Delivery 主要用于发信，不提供收件邮箱托管。",
            "域名配置完成后会显示域名和发件人状态。",
            "测试发信用于验证发件人配置。",
            "计费以当前 OCI 价格页为准。",
        ],
    }


def create_sender(tenant_cfg: dict[str, Any], email_address: str) -> dict[str, str]:
    normalized_email = validate_email_address(email_address, "发件人邮箱")
    client = get_email_client(tenant_cfg)
    response = client.create_sender(
        oci.email.models.CreateSenderDetails(
            compartment_id=tenant_cfg["tenant_id"],
            email_address=normalized_email,
        )
    )
    sender = response.data
    return {
        "id": getattr(sender, "id", ""),
        "email_address": getattr(sender, "email_address", normalized_email),
    }


def create_email_domain(tenant_cfg: dict[str, Any], domain_name: str, description: str = "") -> dict[str, str]:
    normalized = (domain_name or "").strip().lower()
    if not normalized or not re.fullmatch(r"[a-z0-9][a-z0-9.-]+\.[a-z]{2,}", normalized):
        raise ValueError("域名格式不正确。")
    response = get_email_client(tenant_cfg).create_email_domain(
        oci.email.models.CreateEmailDomainDetails(
            compartment_id=tenant_cfg["tenant_id"],
            name=normalized,
            description=(description or "").strip() or None,
        )
    )
    domain = response.data
    return {"id": getattr(domain, "id", ""), "name": getattr(domain, "name", normalized)}


def delete_sender(tenant_cfg: dict[str, Any], sender_id: str) -> None:
    client = get_email_client(tenant_cfg)
    client.delete_sender(sender_id)


def send_test_email(
    tenant_cfg: dict[str, Any],
    sender_email: str,
    to_email: str,
    subject: str,
    body_text: str,
    reply_to: str = "",
) -> dict[str, Any]:
    normalized_sender = validate_email_address(sender_email, "发件人邮箱")
    normalized_to = validate_email_address(to_email, "收件人邮箱")
    normalized_reply = validate_email_address(reply_to, "回复邮箱") if reply_to.strip() else ""
    clean_subject = subject.strip() or "OCI 邮件测试"
    clean_body = body_text.strip() or "这是一封来自 OCI 管理器的测试邮件。"
    body_html = "<pre style='font-family:Consolas,monospace;white-space:pre-wrap'>" + html.escape(clean_body) + "</pre>"

    client = get_email_dp_client(tenant_cfg)
    details = oci.email_data_plane.models.SubmitEmailDetails(
        sender=oci.email_data_plane.models.Sender(
            compartment_id=tenant_cfg["tenant_id"],
            sender_address=oci.email_data_plane.models.EmailAddress(email=normalized_sender),
        ),
        recipients=oci.email_data_plane.models.Recipients(
            to=[oci.email_data_plane.models.EmailAddress(email=normalized_to)]
        ),
        subject=clean_subject,
        body_text=clean_body,
        body_html=body_html,
        reply_to=[oci.email_data_plane.models.EmailAddress(email=normalized_reply)] if normalized_reply else None,
    )
    response = client.submit_email(details)
    payload = response.data
    suppressed = [getattr(item, "email", "") for item in getattr(payload, "suppressed_recipients", []) or []]
    return {
        "message_id": getattr(payload, "message_id", "") or "",
        "envelope_id": getattr(payload, "envelope_id", "") or "",
        "suppressed_recipients": [item for item in suppressed if item],
    }
