"""第六批功能补齐回归：对象列表分页、邮件域名删除。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


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


def test_multipart_lifecycle():
    """init → 逐片上传 → commit 的服务层编排。"""
    import object_storage_service as oss

    client = MagicMock()
    client.get_namespace.return_value.data = "myns"
    client.create_multipart_upload.return_value.data.upload_id = "upid"
    client.upload_part.return_value.data.etag = "etag1"
    cfg = {"tenant_id": "ocid1.tenancy.oc1..mp", "_tenant_name": "t"}
    oss._NAMESPACE_CACHE.clear()

    with patch.object(oss, "get_object_storage_client", return_value=client):
        info = oss.init_multipart(cfg, "bucket1", "big/file.iso")
        blob = MagicMock()
        blob.tell.return_value = 7
        oss.upload_multipart_part(cfg, info["token"], 1, blob, 7)
        result = oss.commit_multipart(cfg, info["token"])

    assert result["parts"] == 1
    create_args = client.create_multipart_upload.call_args[0]
    assert create_args[0] == "myns" and create_args[1] == "bucket1"
    commit_details = client.commit_multipart_upload.call_args[0][4]
    assert commit_details.parts_to_commit[0].part_num == 1

    # commit 后会话已销毁：重复 commit 报"会话不存在"
    with patch.object(oss, "get_object_storage_client", return_value=client):
        with pytest.raises(ValueError, match="会话不存在"):
            oss.commit_multipart(cfg, info["token"])
    oss._NAMESPACE_CACHE.clear()


def test_multipart_abort_cleans_session():
    import object_storage_service as oss

    client = MagicMock()
    client.get_namespace.return_value.data = "myns"
    client.create_multipart_upload.return_value.data.upload_id = "upid2"
    cfg = {"tenant_id": "ocid1.tenancy.oc1..mp2", "_tenant_name": "t"}
    oss._NAMESPACE_CACHE.clear()
    with patch.object(oss, "get_object_storage_client", return_value=client):
        info = oss.init_multipart(cfg, "bucket1", "file.bin")
        oss.abort_multipart(cfg, info["token"])
    client.abort_multipart_upload.assert_called_once()
    assert info["token"] not in oss._MULTIPART_SESSIONS
    oss._NAMESPACE_CACHE.clear()


def test_multipart_session_isolated_by_tenant():
    import object_storage_service as oss

    oss._MULTIPART_SESSIONS["tok"] = {"tenant_id": "tenant-a", "parts": []}
    with pytest.raises(ValueError, match="会话不存在"):
        oss._multipart_session("tok", {"tenant_id": "tenant-b"})
    oss._MULTIPART_SESSIONS.pop("tok", None)


def test_delete_objects_under_prefix_batches():
    import object_storage_service as oss

    client = MagicMock()
    client.get_namespace.return_value.data = "myns"
    page1 = MagicMock()
    page1.data.objects = [MagicMock(name=n) for n in [f"p/{i}" for i in range(1000)]]
    page1.data.next_start_with = None
    client.list_objects.return_value = page1
    cfg = {"tenant_id": "ocid1.tenancy.oc1..pd", "_tenant_name": "t"}
    oss._NAMESPACE_CACHE.clear()
    with patch.object(oss, "get_object_storage_client", return_value=client):
        deleted = oss.delete_objects_under_prefix(cfg, "bucket1", "p/")
    assert deleted == 1000
    assert client.delete_objects.call_count == 10  # 1000 个 / 每批 100
    oss._NAMESPACE_CACHE.clear()


def test_reclaim_p95():
    from monitoring_service import _p95

    assert _p95([]) is None
    assert _p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == 10
    assert _p95([0] * 9 + [50]) == 50


def test_idle_reclaim_risk_flags_low_utilization():
    import idle_service

    instance = MagicMock()
    instance.id = "ocid1.instance.oc1..idle"
    instance.lifecycle_state = "RUNNING"
    instance.shape = "VM.Standard.A1.Flex"
    instance.display_name = "idle-vm"
    instance.time_created = None
    with patch.object(idle_service, "get_compute_client", return_value=MagicMock()), \
         patch.object(idle_service, "get_identity_client", return_value=MagicMock()), \
         patch.object(idle_service, "list_all", side_effect=[[MagicMock(name="AD-1")], [instance]]), \
         patch("monitoring_service.reclaim_percentiles", return_value={instance.id: {"cpu_p95": 3.0, "mem_p95": 5.0, "net_p95": 100.0}}):
        rows = idle_service._reclaim_risk_instances({"tenant_id": "t"})
    assert len(rows) == 1 and rows[0]["kind"] == "reclaim_risk"
    # CPU 分位高于 20% 时不标记
    with patch.object(idle_service, "get_compute_client", return_value=MagicMock()), \
         patch.object(idle_service, "get_identity_client", return_value=MagicMock()), \
         patch.object(idle_service, "list_all", side_effect=[[MagicMock(name="AD-1")], [instance]]), \
         patch("monitoring_service.reclaim_percentiles", return_value={instance.id: {"cpu_p95": 45.0, "mem_p95": 5.0, "net_p95": 100.0}}):
        assert idle_service._reclaim_risk_instances({"tenant_id": "t"}) == []


def test_batch_power_action_route(app_client):
    """批量操作：逐台校验状态，不可操作记为 skipped。"""
    client = MagicMock()
    # 线程并发下 side_effect 列表的消耗顺序不确定，按 id 决定状态
    client.get_instance.side_effect = lambda oid: MagicMock(
        data=MagicMock(lifecycle_state="STOPPED" if oid == "id-a" else "RUNNING")
    )
    with patch("tenant_routes.get_compute_client", return_value=client):
        resp = app_client.post(
            "/tenant/testtenant/instances/batch-action",
            data={"action": "start", "instance_id": ["id-a", "id-b"], "csrf_token": "testtoken"},
            follow_redirects=True,
        )
    body = resp.get_data(as_text=True)
    assert "批量启动" in body
    client.instance_action.assert_called_once_with("id-a", "START")  # 只有 id-a 真正下发


def test_batch_power_action_rejects_hard_actions(app_client):
    resp = app_client.post(
        "/tenant/testtenant/instances/batch-action",
        data={"action": "reset", "instance_id": ["id-a"], "csrf_token": "testtoken"},
        follow_redirects=True,
    )
    assert "批量操作仅支持" in resp.get_data(as_text=True)


def test_token_gate_blocks_without_token():
    """websockify 无鉴权：门禁必须在无/错 token 时拒绝转发。"""
    import socket
    import threading
    import time

    from vnc_tunnel import _TokenGate

    backend = socket.socket()
    backend.bind(("127.0.0.1", 0))
    backend.listen(4)
    backend_port = backend.getsockname()[1]
    received = []

    def serve():
        while True:
            try:
                conn, _ = backend.accept()
            except OSError:
                return
            data = conn.recv(65536)
            received.append(data)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
            conn.close()

    threading.Thread(target=serve, daemon=True).start()

    gate = _TokenGate("127.0.0.1", 0, backend_port, "secret123")
    gate.start()
    try:
        time.sleep(0.2)
        port = gate._sock.getsockname()[1]

        def probe(request):
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
            s.sendall(request)
            s.settimeout(2)
            try:
                return s.recv(65536)
            except OSError:
                return b""
            finally:
                s.close()

        assert b"101" in probe(b"GET /?token=secret123 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert probe(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n") == b""
        assert probe(b"GET /?token=wrong HTTP/1.1\r\nHost: x\r\n\r\n") == b""
        assert len(received) == 1  # 只有一次通过门禁到达后端
    finally:
        gate.stop()
        backend.close()
