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
