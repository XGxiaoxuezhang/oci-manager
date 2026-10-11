"""路由冒烟：全部主要页面在登录态 + OCI mock 下应正常渲染。"""

from __future__ import annotations


PUBLIC_PAGES = [
    "/",
    "/tenants",
    "/tenant/add",
    "/settings",
    "/audit",
    "/instances",
    "/system",
    "/backup",
    "/checks",
    "/cost",
    "/account",
]

# 需要租户上下文的页面（?tenant= 让部分页面选中测试租户）
TENANT_PAGES = [
    "/tenant/testtenant/instances",
    "/tenant/testtenant/public-ips",
    "/tenant/testtenant/console-history",
]


def test_login_page_renders(anon_client):
    response = anon_client.get("/login")
    assert response.status_code == 200
    assert "登录" in response.get_data(as_text=True)


def test_login_required_redirects(anon_client):
    response = anon_client.get("/tenants")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_healthz(anon_client):
    response = anon_client.get("/healthz")
    assert response.status_code == 200


def test_public_pages(app_client):
    for page in PUBLIC_PAGES:
        response = app_client.get(page)
        assert response.status_code == 200, f"{page} -> {response.status_code}"


def test_instance_page_renders(app_client):
    response = app_client.get("/tenant/testtenant/instances")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "test-vm" in body
    # 定时计划面板存在
    assert "定时计划" in body
    # 刷新按钮存在
    assert "refresh=1" in body


def test_console_history_page(app_client):
    response = app_client.get("/tenant/testtenant/console-history")
    assert response.status_code == 200
    assert "启动日志" in response.get_data(as_text=True)
