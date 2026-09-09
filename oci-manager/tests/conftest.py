"""pytest 测试夹具。

关键点：
- 必须在 import settings 之前设置 OCI_MANAGER_DATA_DIR，指向 tests/data
  （settings.py 在模块级读环境变量，晚了不生效）。
- 测试租户的密钥文件不存在也没关系 —— find_tenant_config 只在 fallback
  存在时才修正路径，纯读不影响。
- 不真实调用 OCI：租户页面 mock 掉 service 层函数。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).parent
PROJECT_DIR = TESTS_DIR.parent
TEST_DATA = TESTS_DIR / "data"

os.environ["OCI_MANAGER_DATA_DIR"] = str(TEST_DATA)
sys.path.insert(0, str(PROJECT_DIR))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _prepare_data_dir():
    """写入最小数据文件：一个测试租户 + 一份登录凭据占位。"""
    TEST_DATA.mkdir(parents=True, exist_ok=True)
    tenants_file = TEST_DATA / "tenants.yaml"
    if not tenants_file.exists():
        tenants_file.write_text(
            "testtenant:\n"
            "  tenant_id: ocid1.tenancy.oc1..testtenant\n"
            "  user_id: ocid1.user.oc1..testuser\n"
            "  fingerprint: aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99\n"
            "  region: ap-tokyo-1\n"
            "  key_path: /nonexistent/test.pem\n",
            encoding="utf-8",
        )
    yield
    # session 结束后清理运行时产物（app.db 等），保留 tenants.yaml 便于复跑
    for name in ("app.db", "app.db-wal", "app.db-shm"):
        path = TEST_DATA / name
        if path.exists():
            path.unlink()


@pytest.fixture()
def app_client():
    """带登录态的 test_client，同时 mock 掉全部 OCI 相关 service。"""
    from unittest.mock import patch

    from app import app as flask_app

    flask_app.config["TESTING"] = True

    instance_row = {
        "id": "ocid1.instance.oc1.ap-tokyo-1.aaaaaaaaaaa",
        "name": "test-vm",
        "shape": "VM.Standard.A1.Flex",
        "state": "RUNNING",
        "state_kind": "running",
        "actions": ["softstop", "softreset"],
        "can_terminate": True,
        "availability_domain": "XZlr:AP-TOKYO-1-AD-1",
        "public_ip": "1.2.3.4",
        "private_ip": "10.0.0.2",
        "ipv6": "-",
        "created": "2026-09-01 12:00",
    }

    with patch("tenant_routes.list_instance_rows", return_value=[instance_row]), \
         patch("tenant_routes.launch_context", return_value={"tasks": [], "preset": None}), \
         patch("tenant_routes.instance_overview_context", return_value={"overview_rows": [], "overview_totals": {"instances": 1, "running": 1, "tenants": 1}}):
        with flask_app.test_client() as client:
            with client.session_transaction() as session:
                session["authenticated"] = True
                session["username"] = "admin"
                session["csrf_token"] = "testtoken"
            yield client


@pytest.fixture()
def anon_client():
    """未登录的 test_client。"""
    from app import app as flask_app

    flask_app.config["TESTING"] = True
    with flask_app.test_client() as client:
        yield client
