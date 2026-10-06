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
