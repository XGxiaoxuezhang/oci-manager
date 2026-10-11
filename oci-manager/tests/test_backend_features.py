"""第六批功能补齐回归：对象列表分页、邮件域名删除。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _fake_objects_page(names, next_start_with=None):
    response = MagicMock()
    objects = []
    for n in names:
        obj = MagicMock()  # 注意 name= 是 MagicMock 的特殊 kwarg，必须显式赋值
        obj.name = n
        obj.size = 10
        obj.storage_tier = "Standard"
        obj.time_created = None
        obj.time_modified = None
        obj.etag = "e"
        objects.append(obj)
    response.data.objects = objects
    response.data.next_start_with = next_start_with
    return response


def test_storage_context_paginates_all_objects():
    """回归：list_objects 曾只取第一页，大 bucket 列表和统计静默截断。"""
    import object_storage_service

    client = MagicMock()
    client.get_namespace.return_value.data = "myns"
    client.list_buckets.return_value = MagicMock()
    with patch("object_storage_service.list_all", return_value=[]), \
         patch("object_storage_service.get_object_storage_client", return_value=client):
        client.list_objects.side_effect = [
            _fake_objects_page(["a", "b"], next_start_with="b"),
            _fake_objects_page(["c"]),
        ]
        context = object_storage_service.storage_context(
            {"tenant_id": "ocid1.tenancy.oc1..x", "_tenant_name": "t"}, namespace="myns", bucket_name="b1"
        )

    assert [row["name"] for row in context["objects"]] == ["a", "b", "c"]
    assert context["selected_stats"]["object_count"] == 3
    assert context["objects_truncated"] is False
    assert client.list_objects.call_count == 2


def test_storage_context_marks_truncation_cap():
    """超过 20 页（2 万条）时停止翻页并打上截断标记。"""
    import object_storage_service

    client = MagicMock()
    client.get_namespace.return_value.data = "myns"
    with patch("object_storage_service.list_all", return_value=[]), \
         patch("object_storage_service.get_object_storage_client", return_value=client):
        client.list_objects.side_effect = [
            _fake_objects_page([f"o{i}"], next_start_with=f"o{i}") for i in range(30)
        ]
        context = object_storage_service.storage_context(
            {"tenant_id": "ocid1.tenancy.oc1..x", "_tenant_name": "t"}, namespace="myns", bucket_name="b1"
        )

    assert client.list_objects.call_count == 20
    assert context["objects_truncated"] is True


def test_get_namespace_is_cached_per_tenant():
    import object_storage_service

    client = MagicMock()
    client.get_namespace.return_value.data = "myns"
    object_storage_service._NAMESPACE_CACHE.clear()
    cfg = {"tenant_id": "ocid1.tenancy.oc1..ns", "_tenant_name": "t"}
    first = object_storage_service._cached_namespace(client, cfg)
    second = object_storage_service._cached_namespace(client, cfg)
    assert first == second == "myns"
    assert client.get_namespace.call_count == 1
    object_storage_service._NAMESPACE_CACHE.clear()


def test_delete_email_domain_cleans_pending_dkim():
    """非 ACTIVE 的 DKIM 会挡住域名删除，删除前应先清掉。"""
    import email_service

    client = MagicMock()
    pending = MagicMock(lifecycle_state="PENDING", id="dkim1")
    active = MagicMock(lifecycle_state="ACTIVE", id="dkim2")
    with patch.object(email_service, "get_email_client", return_value=client), \
         patch.object(email_service, "list_all", return_value=[pending, active]):
        email_service.delete_email_domain({"tenant_id": "t"}, "domain1")

    client.delete_dkim.assert_called_once_with("dkim1")
    client.delete_email_domain.assert_called_once_with("domain1")


def test_a1_free_limits_halved():
    """2026 起 Oracle 将 A1 免费额度减半（2 OCPU / 12 GB），旧值 4/24 会导致越限不告警。"""
    from cost_service import FREE_LIMITS

    assert FREE_LIMITS["a1_ocpus"] == 2.0
    assert FREE_LIMITS["a1_memory_gbs"] == 12.0


def test_arm4c24g_preset_removed():
    """4C24G 预设在新额度下创建即超限计费，已移除。"""
    from settings import LAUNCH_PRESETS

    assert "arm4c24g" not in LAUNCH_PRESETS
    assert set(LAUNCH_PRESETS) == {"amd1c1g", "arm2c12g"}


def test_retryable_keywords_no_bare_capacity():
    """回归：裸 "capacity" 曾把 boot volume 类 400 错误也重试 60 次。"""
    from launch_manager import is_retryable_launch_error

    assert not is_retryable_launch_error(Exception("boot volume size exceeded its capacity"))
    assert not is_retryable_launch_error(Exception("no available capacities"))
    assert is_retryable_launch_error(Exception("Out of host capacity."))
    assert is_retryable_launch_error(Exception("too many requests"))
    err = Exception("too many requests")
    err.status = 429
    assert is_retryable_launch_error(err)


def test_schedule_stop_uses_softstop():
    """回归：定时 stop 曾用 STOP（强制断电），有文件系统损坏风险。"""
    from unittest.mock import MagicMock, patch

    import schedule_service

    client = MagicMock()
    client.get_instance.return_value.data.lifecycle_state = "RUNNING"
    with patch("oci_helpers.get_compute_client", return_value=client):
        outcome = schedule_service._execute_schedule({"tenant_id": "t"}, {"action": "stop", "instance_id": "i1"})
    assert outcome == "ok"
    client.instance_action.assert_called_once_with("i1", "SOFTSTOP")


def test_schedule_skips_transition_states():
    import schedule_service

    client = MagicMock()
    client.get_instance.return_value.data.lifecycle_state = "STOPPING"
    with patch("oci_helpers.get_compute_client", return_value=client):
        outcome = schedule_service._execute_schedule({"tenant_id": "t"}, {"action": "stop", "instance_id": "i1"})
    assert outcome == "skipped"
    client.instance_action.assert_not_called()


def test_disable_schedules_for_instance():
    from db import connect, bootstrap
    from schedule_service import add_schedule, disable_schedules_for_instance, list_schedules

    bootstrap()
    sid = add_schedule("testtenant", "ocid1.instance.oc1..kill", "vm", "stop", "01:00", "all")
    try:
        assert disable_schedules_for_instance("testtenant", "ocid1.instance.oc1..kill") == 1
        rows = {r["id"]: r for r in list_schedules("testtenant")}
        assert rows[sid]["enabled"] == 0
    finally:
        from schedule_service import remove_schedule

        remove_schedule("testtenant", sid)


def test_resize_volume_rejects_below_50(app_client):
    """回归：数据卷/引导卷最小 50 GB，此前允许 1GB 的注定失败请求。"""
    resp = app_client.post(
        "/tenant/testtenant/volume/ocid1.volume.oc1..x/resize",
        data={"size_gbs": "10", "current_size": "47", "csrf_token": "testtoken"},
    )
    assert resp.status_code == 302
    resp2 = app_client.post(
        "/tenant/testtenant/volume/ocid1.volume.oc1..x/resize",
        data={"size_gbs": "10", "current_size": "47", "csrf_token": "testtoken"},
        follow_redirects=True,
    )
    assert "50 ~ 32768" in resp2.get_data(as_text=True)
