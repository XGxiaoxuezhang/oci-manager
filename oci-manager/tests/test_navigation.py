"""租户导航回归：工作区页面、最近租户缺省、全局页即时切换。"""

from __future__ import annotations

from unittest.mock import patch


def test_workspace_page_renders(app_client):
    with patch("tenant_routes.workspace_context", return_value={
        "usage_items": [{"key": "a1_ocpus", "label": "A1 OCPU", "used": 2, "limit": 4, "unit": "OCPU", "percent": 50, "status": "ok"}],
        "risks": [],
        "recent_tasks": [],
        "recent_events": [],
    }):
        resp = app_client.get("/tenant/testtenant/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "租户工作区" in body
    assert "/tenant/testtenant/instances" in body
    assert "模块入口" in body


def test_workspace_tenant_redirects_when_missing(anon_client):
    resp = anon_client.get("/tenant/nope/")
    assert resp.status_code == 302


def test_last_tenant_defaults_nav(app_client):
    """回归：全局页落地时侧栏曾经一律灰掉，现在应缺省指向最近租户。"""
    with app_client.session_transaction() as session:
        session["last_tenant"] = "testtenant"
    resp = app_client.get("/settings")
    body = resp.get_data(as_text=True)
    assert '/tenant/testtenant/"' in body.replace("workspace", "") or "/tenant/testtenant/" in body
    # 侧栏下拉里最近租户应处于选中态
    assert 'value="testtenant" selected' in body


def test_cost_page_follows_and_remembers_last_tenant(app_client):
    """回归：cost 页曾有独立的"下拉+应用"交互，且缺省永远取第一个租户。"""
    with app_client.session_transaction() as session:
        session["last_tenant"] = "testtenant"
    # 不带参数也应选中最近租户（OCI 失败没关系，页面仍渲染 selected_tenant）
    resp = app_client.get("/cost")
    body = resp.get_data(as_text=True)
    assert 'value="testtenant" selected' in body
    assert "应用" not in body  # 旧的"下拉+应用"两段式交互已移除
