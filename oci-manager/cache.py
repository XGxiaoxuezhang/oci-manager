"""线程安全的 TTL 缓存，用于「读多写少」的 OCI 列表查询。

key 布局为 (逻辑名, tenant_name, compartment_id)。写操作后调 invalidate_tenant()
清该租户全部缓存；请求带 ?refresh=1 时路由设 g.cache_bypass = True 绕过。
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_store: dict[tuple[str, str, str], tuple[float, Any]] = {}
# 每个 key 的默认存活秒数
DEFAULT_TTL = 90


def cache_get(key: tuple[str, str, str]):
    """命中返回 (True, value)；过期/不存在返回 (False, None)。"""
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return False, None
        expires, value = entry
        if time.monotonic() >= expires:
            _store.pop(key, None)
            return False, None
        return True, value


def cache_set(key: tuple[str, str, str], value: Any, ttl: int = DEFAULT_TTL) -> None:
    with _lock:
        _store[key] = (time.monotonic() + ttl, value)


def invalidate_tenant(tenant_name: str) -> None:
    """清某租户的全部缓存（写操作后调用）。

    键的第 2 段才是租户名（第 1 段是 "instances" 这类逻辑名）；
    之前按 k[0] 匹配永远落空，等于写操作后从不失效缓存。
    """
    with _lock:
        for key in [k for k in _store if len(k) > 1 and k[1] == tenant_name]:
            _store.pop(key, None)


def cache_size() -> int:
    with _lock:
        return len(_store)


def tenant_cached(tenant_cfg: dict[str, Any], name: str, loader, bypass: bool | None = None,
                  ttl: int = DEFAULT_TTL, args: tuple = (), kwargs: dict | None = None):
    """按「租户 + compartment + 逻辑名」缓存的便捷入口。

    bypass 默认从 flask.g.cache_bypass 取（?refresh=1），路由无需再传。
    """
    try:
        from flask import g

        if bypass is None:
            bypass = bool(getattr(g, "cache_bypass", False))
    except RuntimeError:
        bypass = bool(bypass)
    tenant_name = tenant_cfg.get("_tenant_name") or tenant_cfg.get("tenant_id", "-")
    compartment = tenant_cfg.get("_compartment_id") or tenant_cfg.get("tenant_id", "-")
    return cached((name, tenant_name, compartment), loader, bypass=bypass, ttl=ttl, args=args, kwargs=kwargs)


def cached(key: tuple[str, str, str], loader, bypass: bool = False, ttl: int = DEFAULT_TTL,
           args: tuple = (), kwargs: dict | None = None):
    """标准三段式：查缓存 → miss 时加载 → 写缓存。

    bypass=True 跳过缓存读取、直接加载，但结果仍回写缓存——
    强制刷新后下一次普通请求立刻拿到新值。loader 抛异常时不写缓存。

    loader 的参数通过 args/kwargs 显式传入：避免调用方忘记包 lambda
    直接传函数对象（曾因漏包导致 TypeError）。
    """
    if not bypass:
        hit, value = cache_get(key)
        if hit:
            return value
    value = loader(*args, **(kwargs or {}))
    cache_set(key, value, ttl)
    return value
