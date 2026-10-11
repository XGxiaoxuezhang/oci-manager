"""第四批后端正确性修复回归测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_invalidate_tenant_matches_actual_key_order():
    """回归：invalidate_tenant 曾按 k[0]（逻辑名）匹配，等于从不失效缓存。"""
    import cache

    cache.cache_set(("instances", "tenant-a", "c1"), {"old": True}, ttl=60)
    cache.cache_set(("instances", "tenant-b", "c1"), {"keep": True}, ttl=60)
    cache.invalidate_tenant("tenant-a")
    assert cache.cache_get(("instances", "tenant-a", "c1")) == (False, None)
    assert cache.cache_get(("instances", "tenant-b", "c1")) == (True, {"keep": True})
    cache.invalidate_tenant("tenant-b")


def test_bypass_still_writes_cache():
    """回归：?refresh=1 曾完全不写缓存，刷新后普通请求拿到的还是旧值。"""
    import cache

    key = ("instances", "refresh-t", "c")
    value = cache.cached(key, lambda: {"fresh": 1}, bypass=True)
    assert value == {"fresh": 1}
    hit, cached_value = cache.cache_get(key)
    assert hit and cached_value == {"fresh": 1}
    cache.invalidate_tenant("refresh-t")


def test_norm_time_rejects_out_of_range():
    """回归："99:99" 曾归一化成功，计划静默永不执行。"""
    from schedule_service import _norm_time

    assert _norm_time("99:99") == ""
    assert _norm_time("24:00") == ""
    assert _norm_time("3:5") == "03:05"
    assert _norm_time("23:59") == "23:59"
    assert _norm_time("bad") == ""


def test_replace_public_ip_handles_ephemeral():
    """回归：旧 IP 是 EPHEMERAL（控制台默认）时换 IP 会因不解绑而失败。"""
    import tenant_services

    compute_client = MagicMock()
    compute_client.get_instance.return_value.data.display_name = "vm"
    network_client = MagicMock()
    vnic = MagicMock(public_ip="1.2.3.4", id="vnic1", subnet_id="subnet1")
    private_ip = MagicMock(id="pip1")
    ephemeral = MagicMock(id="eip1", ip_address="1.2.3.4", lifetime="EPHEMERAL", assigned_entity_id="pip1")

    with patch.object(tenant_services, "get_compute_client", return_value=compute_client), \
         patch.object(tenant_services, "get_network_client", return_value=network_client), \
         patch.object(tenant_services, "primary_vnic", return_value=vnic), \
         patch.object(tenant_services, "primary_private_ip", return_value=private_ip), \
         patch.object(tenant_services, "list_all", side_effect=[[], [ephemeral]]):
        new_ip, info = tenant_services.replace_public_ip({"tenant_id": "ocid1.tenancy.oc1..x"}, "inst1", release_old=True)

    # ephemeral 不能解绑，应直接删除后建新 IP
    network_client.delete_public_ip.assert_called_once_with("eip1")
    network_client.update_public_ip.assert_not_called()
    args = network_client.create_public_ip.call_args[0][0]
    assert args.private_ip_id == "pip1"
    assert info["released"] == ["1.2.3.4"]


def test_replace_public_ip_reserved_still_unbinds_first():
    import tenant_services

    compute_client = MagicMock()
    compute_client.get_instance.return_value.data.display_name = "vm"
    network_client = MagicMock()
    vnic = MagicMock(public_ip="5.6.7.8", id="vnic1", subnet_id="subnet1")
    private_ip = MagicMock(id="pip1")
    reserved = MagicMock(id="rip1", ip_address="5.6.7.8", lifetime="RESERVED", assigned_entity_id="pip1")

    with patch.object(tenant_services, "get_compute_client", return_value=compute_client), \
         patch.object(tenant_services, "get_network_client", return_value=network_client), \
         patch.object(tenant_services, "primary_vnic", return_value=vnic), \
         patch.object(tenant_services, "primary_private_ip", return_value=private_ip), \
         patch.object(tenant_services, "list_all", side_effect=[[reserved], []]):
        tenant_services.replace_public_ip({"tenant_id": "ocid1.tenancy.oc1..x"}, "inst1", release_old=True)

    network_client.update_public_ip.assert_called_once()
    network_client.delete_public_ip.assert_called_once_with("rip1")


def test_snapshot_wires_budget_alerts():
    """回归：fire_budget_alerts 从未被调用，越限告警从未生效。"""
    import cost_snapshot_service
    from db import bootstrap

    bootstrap()  # 独立跑这个测试时 app 还没导入，先建表
    cfg = {"tenant_id": "ocid1.tenancy.oc1..x", "user_id": "u", "fingerprint": "f", "region": "r", "key_path": "k"}
    ctx = {"free_items": [{"key": "instances", "label": "实例", "used": 9, "limit": 4, "unit": "台", "status": "over"}]}

    # 快照函数内部是延迟导入，patch 必须打在源模块上
    with patch("oci_helpers.find_tenant_config", return_value=cfg), \
         patch("cost_service.free_tier_context", return_value=ctx), \
         patch("cost_service.fire_budget_alerts") as fire, \
         patch("timeout_utils.run_with_timeout", side_effect=lambda t, f, a: f(a)):
        results = cost_snapshot_service.snapshot_all_tenants()

    fire.assert_called_once_with(list(__import__("storage").load_tenants())[0], ctx)
    assert len(results) == 1 and "items" in results[0]
