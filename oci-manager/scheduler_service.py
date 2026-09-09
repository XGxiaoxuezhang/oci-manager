"""后台调度：一个 daemon 线程每分钟 tick，按注册表分发周期任务。

承载三类常驻任务：
- 租户巡检（每小时，结果变化推通知）
- 资源定时启停（每分钟匹配计划表）
- 成本每日快照（每天一次）

单任务异常只记录，不中断调度线程。环境变量 OCI_MANAGER_SCHEDULER=0 可整体关闭。
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime
from typing import Callable

logger = logging.getLogger("scheduler")

_TICK_SECONDS = 30
_started = False
_start_lock = threading.Lock()


def start_scheduler() -> None:
    """启动调度线程（幂等，多次调用只会启动一个）。"""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(target=_run, name="scheduler", daemon=True)
    thread.start()


def _run() -> None:
    # 启动后先等一小段时间，让 Flask 完成初始化
    time.sleep(10)
    state = {"hour_mark": "", "day_mark": ""}
    while True:
        try:
            now = datetime.now()
            _tick(now, state)
        except Exception:  # pragma: no cover - 兜底，绝不让调度线程死掉
            logger.error("调度 tick 异常:\n%s", traceback.format_exc())
        time.sleep(_TICK_SECONDS)


def _tick(now: datetime, state: dict[str, str]) -> None:
    # 每分钟：定时启停
    _safe_run("resource_schedules", _run_resource_schedules)

    # 每小时整点后首个 tick：巡检
    hour_mark = now.strftime("%Y%m%d%H")
    if state["hour_mark"] != hour_mark:
        state["hour_mark"] = hour_mark
        _safe_run("tenant_checks", _run_tenant_checks)

    # 每天 00:05 后首个 tick：成本快照
    if now.minute >= 5:
        day_mark = now.strftime("%Y%m%d")
        if state["day_mark"] != day_mark:
            state["day_mark"] = day_mark
            _safe_run("cost_snapshot", _run_cost_snapshot)


def _safe_run(name: str, func: Callable[[], None]) -> None:
    try:
        func()
    except Exception:
        logger.error("任务 %s 失败:\n%s", name, traceback.format_exc())


# ---------- 任务实现 ----------


def _run_resource_schedules() -> None:
    from schedule_service import run_due_schedules

    run_due_schedules()


def _run_tenant_checks() -> None:
    """每小时巡检全部租户，与上次结果对比，状态变化推通知。"""
    from check_service import load_checks, run_checks
    from notify import notify

    previous = load_checks() or {}
    prev_map = {item.get("tenant_name"): item.get("status") for item in previous.get("tenants", [])}
    results = run_checks()

    changes: list[str] = []
    for item in results.get("tenants", []):
        name = item.get("tenant_name")
        status = item.get("status")
        prev = prev_map.get(name)
        if prev is not None and prev != status:
            if status == "error":
                changes.append(f"[{name}] 变为不可用: {item.get('message', '')[:120]}")
            else:
                changes.append(f"[{name}] 恢复正常")
        elif status == "error" and prev is None:
            # 首次巡检就失败也值得报，避免密钥静默失效
            changes.append(f"[{name}] 巡检失败: {item.get('message', '')[:120]}")

    if changes:
        notify(
            "OCI 巡检状态变化",
            "\n".join(changes),
            {"tenants": len(results.get("tenants", [])), "changes": len(changes)},
        )


def _run_cost_snapshot() -> None:
    from cost_snapshot_service import snapshot_all_tenants

    snapshot_all_tenants()
