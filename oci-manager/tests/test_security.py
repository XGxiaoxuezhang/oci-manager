"""安全修复回归测试：不碰 OCI、不碰网络（通知地址用 IP 字面量）。"""

from __future__ import annotations

import io
import zipfile

import pytest


def test_normalize_tenant_name_rejects_dot_and_dotdot():
    from storage import normalize_tenant_name

    assert normalize_tenant_name("..") == ""
    assert normalize_tenant_name(".") == ""
    assert normalize_tenant_name(" -..- ") == ""
    assert normalize_tenant_name("ok-name_1.2") == "ok-name_1.2"
    assert normalize_tenant_name("bad name!") == "bad-name"
    # Windows 会吞掉结尾的点，导致目录名与租户名不一致，统一去掉
    assert normalize_tenant_name("trailing..") == "trailing"


def test_remove_tenant_record_cannot_escape_tenant_dir(tmp_path, monkeypatch):
    import tenant_services

    tenant_root = tmp_path / "tenants"
    (tenant_root / "keep").mkdir(parents=True)
    (tenant_root / "keep" / "key.pem").write_text("k", encoding="utf-8")
    marker = tmp_path / "auth.yaml"
    marker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(tenant_services, "TENANT_DIR", tenant_root)

    # 历史漏洞：租户名 ".." 会让 rmtree 清空上级目录
    tenant_services.remove_tenant_record({}, "..")
    assert marker.exists()
    assert (tenant_root / "keep" / "key.pem").exists()

    tenant_services.remove_tenant_record({}, ".")
    assert marker.exists()
    assert tenant_root.exists()

    # 正常删除仍然工作
    tenant_services.remove_tenant_record({}, "keep")
    assert not (tenant_root / "keep").exists()
    assert marker.exists()


def test_create_tenant_record_rejects_escape(tmp_path, monkeypatch):
    import tenant_services

    tenant_root = tmp_path / "tenants"
    tenant_root.mkdir(parents=True)
    monkeypatch.setattr(tenant_services, "TENANT_DIR", tenant_root)

    class FakeKeyFile:
        def save(self, path):  # pragma: no cover - 不应被调用
            raise AssertionError("非法租户名不应触发私钥写入")

    with pytest.raises(ValueError, match="租户名称不合法"):
        tenant_services.create_tenant_record({}, "..", "ocid1.tenancy.oc1..a", "ocid1.user.oc1..a", "us-ashburn-1", "aa" * 16, FakeKeyFile)
    with pytest.raises(ValueError, match="租户名称不合法"):
        tenant_services.create_tenant_record({}, ".", "ocid1.tenancy.oc1..a", "ocid1.user.oc1..a", "us-ashburn-1", "aa" * 16, FakeKeyFile)


def test_vnc_rejects_space_syntax_ssh_option():
    from vnc_tunnel import _reject_unsafe_option

    # 修复前按 "=" 切分，空格语法的 LocalCommand 整串当 key，漏进白名单
    with pytest.raises(ValueError, match="不允许"):
        _reject_unsafe_option("LocalCommand touch /tmp/x")
    with pytest.raises(ValueError, match="不允许"):
        _reject_unsafe_option("PermitLocalCommand yes")
    with pytest.raises(ValueError, match="不允许"):
        _reject_unsafe_option("proxyjump=jump@example.com")
    # OCI 官方连接串必需的选项保持放行
    _reject_unsafe_option("ProxyCommand=nc -X connect %h %p")
    with pytest.raises(ValueError, match="无法识别"):
        _reject_unsafe_option("=weird")


def test_vnc_rejects_ssh_executable_path():
    from vnc_tunnel import _split_ssh_command

    assert _split_ssh_command("ssh -p 443 x@example.com")[0] == "ssh"
    assert _split_ssh_command("SSH")[0] == "SSH"
    for bad in (
        "/usr/bin/ssh -p 443 x@example.com",
        "C:\\Windows\\System32\\OpenSSH\\ssh.exe -p 443 x@example.com",
        "\\\\attacker\\share\\evil\\ssh.exe -p 443 x@example.com",
        "nc -p 443 x@example.com",
    ):
        with pytest.raises(ValueError, match="ssh"):
            _split_ssh_command(bad)


def test_vnc_prepare_command_still_rewrites_oci_string():
    from vnc_tunnel import _prepare_command

    oci_string = (
        'ssh -o ProxyCommand="nc -X connect %h %p -P 443" '
        "-o PubkeyAuthentication=no -i <privateKey> x@example.com -p 443 -L 5900:localhost:5900"
    )
    command = _prepare_command(oci_string, 5999, "/tmp/console_key.pem")
    assert command[0] == "ssh"
    assert "/tmp/console_key.pem" in command
    assert "-i" in command
    # 本地转发端口被改写为服务端分配的端口
    assert any(arg.endswith(f"5999:localhost:5900") for arg in command)
    # ExitOnForwardFailure 等安全选项插在 ssh 之后
    assert "ExitOnForwardFailure=yes" in command


def test_csv_formula_neutralized():
    from export_utils import _neutralize_formula

    assert _neutralize_formula("=cmd|'/c calc'!A0") == "'=cmd|'/c calc'!A0"
    assert _neutralize_formula("@SUM(1)") == "'@SUM(1)"
    assert _neutralize_formula("-5") == "'-5"
    assert _neutralize_formula("+1") == "'+1"
    assert _neutralize_formula("normal-name") == "normal-name"
    # 数值不受影响，避免破坏数字列
    assert _neutralize_formula(-5) == -5
    assert _neutralize_formula(12.5) == 12.5


def test_notification_url_checks():
    from notify import safe_notification_url, valid_notification_url

    assert not valid_notification_url("ftp://example.com/hook")
    assert not valid_notification_url("file:///etc/passwd")
    assert not valid_notification_url("not a url")
    assert valid_notification_url("https://example.com/webhook")

    # 云元数据端点必须被拦截
    assert not safe_notification_url("http://169.254.169.254/latest/meta-data/")
    assert not safe_notification_url("http://[fe80::1]/")
    assert not safe_notification_url("file://127.0.0.1/x")
    # 私网/环回放行（自建 ntfy 场景），IP 字面量不需要 DNS
    assert safe_notification_url("http://127.0.0.1:2586/mytopic")


def test_restore_backup_zip_skips_protected_files(tmp_path, monkeypatch):
    import backup_service

    monkeypatch.setattr(backup_service, "DATA_DIR", tmp_path)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("auth.yaml", "evil: true\n")
        archive.writestr(".secret_key", "attacker-known-secret")
        # 单层 ".." 落点仍在 DATA_DIR 内，靠 resolve 后的落点检查跳过
        archive.writestr("tenants/../settings.yaml", "webhook_url: http://evil.example\n")
        archive.writestr("tenants/keep/key.pem", "private-key-bytes")
        archive.writestr("app.db-wal", "junk")

    restored, skipped = backup_service.restore_backup_zip(payload.getvalue())

    assert not (tmp_path / "auth.yaml").exists()
    assert not (tmp_path / ".secret_key").exists()
    assert not (tmp_path / "settings.yaml").exists()
    assert (tmp_path / "tenants" / "keep" / "key.pem").exists()
    assert set(skipped) == {"auth.yaml", ".secret_key", "tenants/../settings.yaml"}
    assert restored == ["tenants/keep/key.pem"]

    # 双层 ".." 落点在 DATA_DIR 之外，必须中止整个恢复（fail-closed）
    escaping = io.BytesIO()
    with zipfile.ZipFile(escaping, "w") as archive:
        archive.writestr("tenants/../../settings.yaml", "evil")
    with pytest.raises(ValueError, match="越界"):
        backup_service.restore_backup_zip(escaping.getvalue())


def test_totp_rfc6238_vector():
    """RFC 6238 官方测试向量：SHA-1、T=59、种子为 ASCII '12345678901234567890'。"""
    import base64

    import totp as totp_module
    from unittest.mock import patch

    rfc_secret = base64.b32encode(b"12345678901234567890").decode()
    with patch.object(totp_module.time, "time", return_value=59):
        code = totp_module._code_at(rfc_secret, 59 // 30)
    assert code == "287082"
    # verify 允许 ±1 窗口漂移
    with patch.object(totp_module.time, "time", return_value=59):
        assert totp_module.verify(rfc_secret, "287082")
        assert totp_module.verify(rfc_secret, " 287082 ")
        assert not totp_module.verify(rfc_secret, "000000")
        assert not totp_module.verify(rfc_secret, "28708")


def test_totp_rejects_garbage():
    import base64

    import totp as totp_module

    rfc_secret = base64.b32encode(b"12345678901234567890").decode()
    assert not totp_module.verify(rfc_secret, "")
    assert not totp_module.verify(rfc_secret, "abcdef")
    assert not totp_module.verify(rfc_secret, "12345")


def test_two_factor_login_flow(anon_client):
    """2FA 全流程：密码通过 → 待验证态 → 动态码 → 完全登录。"""
    import base64
    import time as time_mod
    from unittest.mock import patch

    import totp as totp_module
    import yaml as yaml_mod
    from pathlib import Path as P

    with anon_client.session_transaction() as session:
        session["csrf_token"] = "testtoken"

    auth_path = P(__file__).parent / "data" / "auth.yaml"
    data = yaml_mod.safe_load(auth_path.read_text(encoding="utf-8")) or {}
    data["must_change_password"] = False
    data["totp_enabled"] = True
    data["totp_secret"] = base64.b32encode(b"12345678901234567890").decode()
    data["last_login_ip"] = ""  # 触发"新来源登录"通知
    auth_path.write_text(yaml_mod.safe_dump(data, allow_unicode=True), encoding="utf-8")
    try:
        # 1) 密码通过 → 进入待验证页
        resp = anon_client.post("/login", data={"username": "admin", "password": "admin123456", "csrf_token": "testtoken"})
        assert resp.status_code == 302 and "/login/totp" in resp.headers["Location"]

        # 2) 未完全登录时访问业务页被挡回
        resp = anon_client.get("/tenants")
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]

        # 3) 错误动态码 → 回到验证页（模拟浏览器：先取页面拿新 CSRF token）
        import re as re_mod
        page = anon_client.get("/login/totp")
        csrf = re_mod.search('name="csrf-token" content="([^"]+)"', page.get_data(as_text=True)).group(1)
        resp = anon_client.post("/login/totp", data={"code": "000000", "csrf_token": csrf})
        assert resp.status_code == 200 and "动态码不正确" in resp.get_data(as_text=True)

        # 4) 正确动态码 → 完全登录（重新取页面刷新 token）
        page = anon_client.get("/login/totp")
        csrf = re_mod.search('name="csrf-token" content="([^"]+)"', page.get_data(as_text=True)).group(1)
        code = totp_module._code_at(data["totp_secret"], int(time_mod.time()) // 30)
        with patch("auth_routes.notify") as notify:
            resp = anon_client.post("/login/totp", data={"code": code, "csrf_token": csrf})
        assert resp.status_code == 302 and "/login" not in resp.headers["Location"]
        with anon_client.session_transaction() as session:
            assert session.get("authenticated") is True
        assert notify.called  # 新来源登录推送通知
    finally:
        data = yaml_mod.safe_load(auth_path.read_text(encoding="utf-8")) or {}
        data.pop("totp_enabled", None)
        data.pop("totp_secret", None)
        data.pop("last_login_ip", None)
        data["must_change_password"] = False
        auth_path.write_text(yaml_mod.safe_dump(data, allow_unicode=True), encoding="utf-8")
