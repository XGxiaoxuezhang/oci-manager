from __future__ import annotations

import html
import re
from email.utils import parseaddr
from typing import Any

import oci

from oci_helpers import build_config, client_kwargs, get_identity_client, list_all
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

    domain_rows = []
    for item in sorted(domains, key=lambda d: getattr(d, "name", "").lower()):
        row = _domain_row(item)
        dkims_summaries = list_all(client.list_dkims, email_domain_id=row["id"])
        full_dkims = []
        for ds in dkims_summaries:
            try:
                full_dkims.append(client.get_dkim(ds.id).data)
            except Exception:
                full_dkims.append(ds)  # fallback to summary if get fails

        row["dkims"] = [
            {
                "id": d.id,
                "name": d.name,
                "state": getattr(d, "lifecycle_state", "-"),
                "dns_subdomain_name": getattr(d, "dns_subdomain_name", ""),
                "cname_record_value": getattr(d, "cname_record_value", ""),
            }
            for d in full_dkims
        ]
        domain_rows.append(row)

    sender_rows = sorted((_sender_row(item) for item in senders), key=lambda row: row["email_address"].lower())

    return {
        "region": tenant_cfg["region"],
        "email_configuration": {
            "http_submit_endpoint": getattr(configuration, "http_submit_endpoint", "") or "-",
            "smtp_submit_endpoint": getattr(configuration, "smtp_submit_endpoint", "") or "-",
        },
        "email_domains": domain_rows,
        "senders": sender_rows,
        "email_stats": {
            "domain_count": len(domain_rows),
            "active_domain_count": sum(1 for item in domain_rows if item["state"] == "ACTIVE"),
            "sender_count": len(sender_rows),
        },
        "email_guides": [
            "OCI Email Delivery 主要用于发信，不提供收件邮箱托管。",
            "你可以在这里直接添加发信域名和发件人，不需要去 OCI 控制台操作。",
            "重要提示：添加域名后，需要在你的域名 DNS 解析中配置好 DKIM/SPF 记录，否则发出的邮件容易进垃圾箱。",
            "测试发信时，收件地址可以直接填你自己的 QQ、Outlook、Gmail 或企业邮箱。",
            "免费额度和计费策略可能变化，请以官方文档和当前价格页为准。",
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


def delete_sender(tenant_cfg: dict[str, Any], sender_id: str) -> None:
    client = get_email_client(tenant_cfg)
    client.delete_sender(sender_id)


def create_domain(tenant_cfg: dict[str, Any], domain_name: str, description: str = "") -> dict[str, str]:
    domain_name = domain_name.strip()
    if not domain_name:
        raise ValueError("域名不能为空。")
    client = get_email_client(tenant_cfg)
    response = client.create_email_domain(
        oci.email.models.CreateEmailDomainDetails(
            compartment_id=tenant_cfg["tenant_id"],
            name=domain_name,
            description=description if description else None,
        )
    )
    domain = response.data
    
    # 尝试自动生成第一个 DKIM，名称固定为 oci 
    try:
        client.create_dkim(
            oci.email.models.CreateDkimDetails(
                email_domain_id=domain.id,
                name="oci"
            )
        )
    except Exception:
        pass
        
    return {
        "id": getattr(domain, "id", ""),
        "name": getattr(domain, "name", domain_name),
    }


def delete_domain(tenant_cfg: dict[str, Any], domain_id: str) -> None:
    client = get_email_client(tenant_cfg)
    client.delete_email_domain(domain_id)


def create_dkim(tenant_cfg: dict[str, Any], domain_id: str, name: str) -> None:
    client = get_email_client(tenant_cfg)
    client.create_dkim(
        oci.email.models.CreateDkimDetails(
            email_domain_id=domain_id,
            name=name
        )
    )


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
    clean_body = body_text.strip() or "这是一封来自 OCI Manager 的测试邮件。"
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


def generate_smtp_credential(tenant_cfg: dict[str, Any]) -> dict[str, str]:
    client = get_identity_client(tenant_cfg)
    response = client.create_smtp_credential(
        user_id=tenant_cfg["user_id"],
        create_smtp_credential_details=oci.identity.models.CreateSmtpCredentialDetails(
            description="Created by OCI Manager"
        )
    )
    cred = response.data
    return {
        "username": getattr(cred, "username", ""),
        "password": getattr(cred, "password", "")
    }
