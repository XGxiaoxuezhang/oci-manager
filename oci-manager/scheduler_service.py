"""后台调度：一个 daemon 线程每分钟 tick，按注册表分发周期任务。

承载三类常驻任务：
- 租户巡检（每小时，结果变化推通知）
- 资源定时启停（每分钟匹配计划表）
- 成本每日快照（每天一次）

单任务异常只记录，不中断调度线程。环境变量 OCI_MANAGER_SCHEDULER=0 可整体关闭；
debug 模式（Werkzeug reloader）下只在真正提供服务的子进程里启动，避免父子进程双份调度。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from datetime import datetime
from typing import Callable

from settings import env_bool

logger = logging.getLogger("scheduler")

_TICK_SECONDS = 30
_started = False
_start_lock = threading.Lock()


def start_scheduler() -> None:
    """启动调度线程（幂等，多次调用只会启动一个）。"""
    global _started
    run_main = os.environ.get("WERKZEUG_RUN_MAIN")
    if run_main is not None and run_main != "true":
        # debug reloader 的父进程只负责监视文件改动，不提供服务，不启动调度
        logger.info("reloader 父进程，跳过调度线程启动")
        return
    if not env_bool("OCI_MANAGER_SCHEDULER", True):
        logger.info("OCI_MANAGER_SCHEDULER=0，调度线程未启动")
        return
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
    # 每分钟：定时启停（必须留在调度线程里，窗口匹配依赖每分钟准时跑）
    _safe_run("resource_schedules", _run_resource_schedules)

    # 每小时整点后首个 tick：巡检（后台线程，慢任务不占调度线程）
    hour_mark = now.strftime("%Y%m%d%H")
    if state["hour_mark"] != hour_mark:
        state["hour_mark"] = hour_mark
        _spawn_once("tenant_checks", _run_tenant_checks)

    # 每天 00:05 后首个 tick：成本快照 + 数据目录自动备份（后台线程）
    if now.minute >= 5:
        day_mark = now.strftime("%Y%m%d")
        if state["day_mark"] != day_mark:
            state["day_mark"] = day_mark
            _spawn_once("cost_snapshot", _run_cost_snapshot)
            _spawn_once("auto_backup", _run_auto_backup)

    # 每小时：SQLite 运行数据清理（audit/login/launch_tasks 保留上限）
    hour_mark = now.strftime("%Y%m%d%H")
    if state["hour_mark"] != hour_mark:
        _spawn_once("db_cleanup", _run_db_cleanup)


def _safe_run(name: str, func: Callable[[], None]) -> None:
    try:
        func()
    except Exception:
        logger.error("任务 %s 失败:\n%s", name, traceback.format_exc())


# 巡检/快照这类长任务（N 租户 × 15~40s）曾直接跑在唯一的调度线程里，
# 租户一多就能吞掉整分钟的定时启停窗口。挪到独立线程，同任务不并发。
_BG_LOCKS: dict[str, threading.Lock] = {
    "tenant_checks": threading.Lock(),
    "cost_snapshot": threading.Lock(),
    "auto_backup": threading.Lock(),
    "db_cleanup": threading.Lock(),
}


def _spawn_once(name: str, func: Callable[[], None]) -> None:
    lock = _BG_LOCKS[name]
    if not lock.acquire(blocking=False):
        logger.info("任务 %s 仍在运行，本轮跳过", name)
        return

    def runner() -> None:
        try:
            func()
        except Exception:
            logger.error("任务 %s 失败:\n%s", name, traceback.format_exc())
        finally:
            lock.release()

    try:
        threading.Thread(target=runner, name=f"bg-{name}", daemon=True).start()
    except Exception:
        lock.release()  # 线程起不来（句柄耗尽等）时必须还锁，否则该任务永久静默跳过
        raise


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
    if results is None:
        # 手动巡检正在进行，本轮跳过（下一小时会有新一轮）
        logger.info("手动巡检进行中，定时巡检本轮跳过")
        return

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


def _run_auto_backup() -> None:
    """每日自动备份数据目录；失败推通知（成功不推，避免每日骚扰）。"""
    from backup_service import run_scheduled_backup
    from notify import notify

    try:
        result = run_scheduled_backup()
        logger.info("自动备份完成: %s (%d bytes)", result["path"], result["size"])
    except Exception as exc:
        logger.error("自动备份失败:\n%s", traceback.format_exc())
        notify("OCI 管理器自动备份失败", str(exc)[:300], {})
        raise


def _run_db_cleanup() -> None:
    from db import cleanup_runtime_rows

    cleanup_runtime_rows()
