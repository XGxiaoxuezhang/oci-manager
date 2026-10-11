"""第三批功能回归：AJAX 电源操作 JSON 分支、CSV 导出、表单失败回填。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_instance_action_json_branch(app_client):
    """fetch 发起的电源操作应返回 JSON（前端局部更新状态徽章）。"""
    client = MagicMock()
    client.get_instance.return_value.data.lifecycle_state = "RUNNING"
    with patch("tenant_routes.get_compute_client", return_value=client):
        resp = app_client.post(
            "/tenant/testtenant/instance/action/ocid1.instance.oc1.x/softstop",
            headers={"X-Requested-With": "fetch", "X-CSRF-Token": "testtoken"},
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    # 提交后立刻查询，lifecycle_state 通常还没变：不假装已变更
    assert data["state"] is None
    assert "软停" in data["message"]
    assert "生效中" in data["message"]


def test_instance_action_json_rejects_invalid_state(app_client):
    client = MagicMock()
    client.get_instance.return_value.data.lifecycle_state = "STOPPED"
    with patch("tenant_routes.get_compute_client", return_value=client):
        resp = app_client.post(
            "/tenant/testtenant/instance/action/ocid1.instance.oc1.x/softstop",
            headers={"X-Requested-With": "fetch", "X-CSRF-Token": "testtoken"},
        )
    data = resp.get_json()
    assert data["ok"] is False
    assert "不允许" in data["message"]


def test_audit_export_csv(app_client):
    resp = app_client.get("/audit/export")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["Content-Type"]
    assert "时间,用户,租户,动作,状态,路径" in resp.get_data(as_text=True)


def test_tenant_export_routes_registered(app_client):
    """导出路由存在；假租户在 OCI 调用失败时回列表（302）。"""
    for url in (
        "/tenant/testtenant/volumes/export",
        "/tenant/testtenant/public-ips/export",
    ):
        resp = app_client.get(url)
        assert resp.status_code == 302, (url, resp.status_code)


def test_add_tenant_repopulates_on_error(app_client):
    resp = app_client.post("/tenant/add", data={"tenant_name": "my-tenant", "csrf_token": "testtoken"})
    assert resp.status_code == 200, "校验失败应带值重渲染而不是跳转"
    body = resp.get_data(as_text=True)
    assert 'value="my-tenant"' in body
    assert "格式不正确" in body


def test_databases_create_repopulates_on_error(app_client):
    resp = app_client.post(
        "/tenant/testtenant/databases/create",
        data={"display_name": "mydb", "db_name": "mydb", "csrf_token": "testtoken"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'value="mydb"' in body


def test_databases_create_success_redirects(app_client):
    """回归：成功分支曾缺 return，创建成功后必然 500。"""
    result = {"id": "ocid1.autonomousdatabase.oc1..x", "display_name": "mydb", "is_free_tier": True}
    with patch("database_routes.create_autonomous_database", return_value=result),          patch("database_routes.spawn_provision_watcher"):
        resp = app_client.post(
            "/tenant/testtenant/databases/create",
            data={
                "display_name": "mydb",
                "db_name": "mydb",
                "admin_password": "Passw0rd123456",
                "csrf_token": "testtoken",
            },
        )
    assert resp.status_code == 302, resp.status_code


def test_fetch_ssh_hint_never_raises():
    """回归：SSH hint 的 client 构建失败曾把任务从 success 翻转成 failed。"""
    import launch_manager

    with patch("launch_manager.get_network_client", side_effect=RuntimeError("key gone")):
        hint = launch_manager._fetch_ssh_hint({"tenant_id": "t"}, "inst1")
    assert "SSH" not in hint
    assert "尚未分配" in hint
