"""第五批后端健壮性回归：client 缓存、错误友好化、定时窗口匹配、并发互斥、日志截断。"""

from __future__ import annotations

import os
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


def test_get_cached_client_reuses_instance(tmp_path):
    from oci_helpers import get_cached_client

    key_file = tmp_path / "key.pem"
    key_file.write_text("fake", encoding="utf-8")
    cfg = {"tenant_id": "t", "user_id": "u", "fingerprint": "f", "region": "r", "key_path": str(key_file)}

    calls = []
    first = get_cached_client("compute", cfg, lambda: calls.append(1) or object())
    second = get_cached_client("compute", cfg, lambda: calls.append(1) or object())
    assert first is second
    assert len(calls) == 1

    # 私钥文件被重写（轮换密钥）后必须换新 client
    os.utime(key_file, (time.time() + 1, time.time() + 1))
    get_cached_client("compute", cfg, lambda: calls.append(1) or object())
    assert len(calls) == 2


def test_friendly_error_maps_service_error():
    import oci
    from oci_helpers import friendly_error

    err = oci.exceptions.ServiceError(409, "Conflict", {}, "already exists")
    text = friendly_error(err)
    assert "冲突" in text and "already exists" in text

    quota = oci.exceptions.ServiceError(429, "TooManyRequests", {}, "slow down")
    assert "限流" in friendly_error(quota)

    # 非 OCI 异常原样透传（因此路由里可以放心全局替换）
    assert friendly_error(ValueError("自己的错误")) == "自己的错误"


def test_schedule_window_match_catches_missed_run():
    """回归：精确匹配当前分钟时，停机跨过计划时间会静默漏跑。"""
    import schedule_service
    from db import connect
    from storage import load_tenants

    tenant_name = sorted(load_tenants().keys())[0]
    schedule_id = schedule_service.add_schedule(tenant_name, "ocid1.instance.oc1..x", "vm", "stop", "00:00", "all")
    try:
        # 模拟 09:30 的 tick：00:00 的计划早已错过，窗口匹配必须补跑
        with patch("oci_helpers.find_tenant_config", return_value={"tenant_id": "t"}), \
             patch("schedule_service.run_with_timeout", side_effect=lambda t, f, *a: f(*a)), \
             patch("schedule_service._execute_schedule", return_value="ok"), \
             patch("schedule_service.notify") as notify:
            executed = schedule_service.run_due_schedules(now=datetime(2026, 10, 4, 9, 30))

        assert len(executed) == 1 and executed[0]["result"] == "ok"
        notify.assert_called_once()
        with connect() as conn:
            row = conn.execute("SELECT last_run_date FROM resource_schedules WHERE id = ?", (schedule_id,)).fetchone()
        assert row["last_run_date"] == "2026-10-04"

        # 当天第二次 tick 不再执行
        with patch("oci_helpers.find_tenant_config", return_value={"tenant_id": "t"}), \
             patch("schedule_service.run_with_timeout", side_effect=lambda t, f, *a: f(*a)), \
             patch("schedule_service._execute_schedule", return_value="ok") as execute:
            assert schedule_service.run_due_schedules(now=datetime(2026, 10, 4, 9, 31)) == []
        execute.assert_not_called()
    finally:
        schedule_service.remove_schedule(tenant_name, schedule_id)


def test_schedule_timeout_notifies_unknown_result():
    """回归：写操作超时曾被报成"执行失败"，诱导用户重复操作。"""
    import schedule_service
    from db import connect
    from storage import load_tenants
    from timeout_utils import TimedCallError

    tenant_name = sorted(load_tenants().keys())[0]
    schedule_id = schedule_service.add_schedule(tenant_name, "ocid1.instance.oc1..x", "vm", "stop", "00:00", "all")
    try:
        with patch("oci_helpers.find_tenant_config", return_value={"tenant_id": "t"}), \
             patch("schedule_service.run_with_timeout", side_effect=TimedCallError("超时")), \
             patch("schedule_service.notify") as notify:
            executed = schedule_service.run_due_schedules(now=datetime(2026, 10, 4, 9, 30))

        assert executed[0]["result"] == "timeout"
        title, message = notify.call_args[0]
        assert "结果未知" in title
        assert "失败" not in title and "失败" not in message
        # 超时也写标记，同一天不会反复跑
        with connect() as conn:
            row = conn.execute("SELECT last_run_date FROM resource_schedules WHERE id = ?", (schedule_id,)).fetchone()
        assert row["last_run_date"] == "2026-10-04"
    finally:
        schedule_service.remove_schedule(tenant_name, schedule_id)


def test_run_checks_busy_returns_none():
    import check_service

    with check_service._RUN_LOCK:
        assert check_service.run_checks() is None


def test_task_log_truncated_to_200():
    import launch_manager

    task_id = "test-truncate"
    launch_manager.LAUNCH_TASKS[task_id] = {"id": task_id, "tenant_name": "testtenant", "status": "running", "logs": [f"l{i}" for i in range(200)]}
    try:
        launch_manager.append_task_log(task_id, "new line")
        logs = launch_manager.LAUNCH_TASKS[task_id]["logs"]
        assert len(logs) == 200
        assert logs[-1].endswith("new line")
    finally:
        launch_manager.LAUNCH_TASKS.pop(task_id, None)
        try:
            with launch_manager.connect() as conn:
                conn.execute("DELETE FROM launch_tasks WHERE id = ?", (task_id,))
        except Exception:
            pass


def test_snapshot_and_checks_survive_reimport(monkeypatch):
    """启动开关：OCI_MANAGER_SCHEDULER=0 时 start_scheduler 不启动。"""
    import scheduler_service

    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    monkeypatch.setenv("OCI_MANAGER_SCHEDULER", "0")
    scheduler_service._started = False
    scheduler_service.start_scheduler()
    assert scheduler_service._started is False
    monkeypatch.delenv("OCI_MANAGER_SCHEDULER")
    scheduler_service.start_scheduler()
    assert scheduler_service._started is True
