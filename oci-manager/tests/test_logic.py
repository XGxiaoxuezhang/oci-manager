"""纯逻辑单元测试：不碰 OCI、不碰 Flask。"""

from __future__ import annotations

from datetime import datetime

from schedule_service import _matches_today, _norm_days, _norm_time, days_label


def test_norm_days():
    assert _norm_days("all") == "all"
    assert _norm_days("") == "all"
    assert _norm_days("每天") == "all"
    assert _norm_days("1,2,3,4,5") == "1,2,3,4,5"
    assert _norm_days("5,1,1,9") == "1,5"  # 去重 + 排序 + 丢弃越界


def test_norm_time():
    assert _norm_time("3:5") == "03:05"
    assert _norm_time("23:59") == "23:59"
    assert _norm_time("bad") == ""


def test_days_label():
    assert days_label("all") == "每天"
    assert days_label("1,2,3,4,5,6,7") == "每天"
    assert days_label("6,7") == "周六/日"
    assert days_label("1") == "周一"


def test_matches_today():
    monday = datetime(2026, 9, 7, 8, 0)  # 周一
    assert _matches_today("all", monday.weekday())
    assert _matches_today("1,2,3", monday.weekday())
    assert not _matches_today("6,7", monday.weekday())
    sunday = datetime(2026, 9, 13, 8, 0)  # 周日
    assert _matches_today("7", sunday.weekday())
    assert not _matches_today("1,2,3,4,5", sunday.weekday())


def test_cache_roundtrip():
    import cache

    key = ("tenant", "compartment", "instances")
    assert cache.cache_get(key) == (False, None)
    cache.cache_set(key, [1, 2, 3], ttl=60)
    hit, value = cache.cache_get(key)
    assert hit and value == [1, 2, 3]
    cache.invalidate_tenant("tenant")
    assert cache.cache_get(key) == (False, None)


def test_cache_expired():
    import time as time_mod

    import cache

    key = ("t2", "c2", "k2")
    cache.cache_set(key, "v", ttl=0)
    time_mod.sleep(0.05)
    assert cache.cache_get(key) == (False, None)


def test_cache_bypass():
    import cache

    calls = []

    def loader():
        calls.append(1)
        return "fresh"

    key = ("t3", "c3", "k3")
    cache.cache_set(key, "stale", ttl=60)
    # bypass 时直接走 loader、且不覆盖缓存
    assert cache.cached(key, loader, bypass=True) == "fresh"
    hit, value = cache.cache_get(key)
    assert hit and value == "stale"
    # 非 bypass 时命中缓存
    assert cache.cached(key, loader, bypass=False) == "stale"
    assert len(calls) == 1
    cache.invalidate_tenant("t3")


def test_cache_loader_with_args():
    """回归测试：cached 应通过 args/kwargs 传参，避免调用方忘包闭包。

    修过 launch_context 的 bug：之前直接传函数对象导致调用时缺参 TypeError。
    """
    import cache

    calls = []

    def loader(value: int) -> str:
        calls.append(value)
        return f"got-{value}"

    key = ("t4", "c4", "k4")
    assert cache.cached(key, loader, args=(42,)) == "got-42"
    # 命中缓存，不再调 loader
    assert cache.cached(key, loader, args=(42,)) == "got-42"
    assert calls == [42]
    cache.invalidate_tenant("t4")
