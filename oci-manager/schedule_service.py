"""资源定时启停：计划的增删改查 + 到点执行。

计划存 SQLite resource_schedules 表；scheduler 每分钟调 run_due_schedules()。
days 存 "1,2,3,4,5"（周一=1..周日=7）或 "all"。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from db import connect
from notify import notify
from storage import now_iso

logger = logging.getLogger("scheduler")

_DAY_LABELS = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
_ACTION_LABELS = {"start": "启动", "stop": "停止"}


def _norm_days(days: str) -> str:
    days = (days or "").strip().lower()
    if days in ("", "all", "每天", "每天".lower()):
        return "all"
    parts = []
    for part in days.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            value = int(part)
            if 1 <= value <= 7:
                parts.append(str(value))
    return ",".join(sorted(set(parts))) if parts else "all"


def _norm_time(value: str) -> str:
    value = (value or "").strip()
    parts = value.split(":")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    return ""


def days_label(days: str) -> str:
    if days == "all":
        return "每天"
    nums = [int(x) for x in days.split(",") if x.isdigit()]
    if len(nums) == 7:
        return "每天"
    return "周" + "/".join(_DAY_LABELS.get(n, str(n)) for n in sorted(nums))


def list_schedules(tenant_name: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM resource_schedules WHERE tenant_name = ? ORDER BY time_hhmm, instance_name",
            (tenant_name,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_schedule(
    tenant_name: str,
    instance_id: str,
    instance_name: str,
    action: str,
    time_hhmm: str,
    days: str,
) -> int | None:
    if action not in ("start", "stop"):
        raise ValueError("动作只能是 start 或 stop。")
    time_hhmm = _norm_time(time_hhmm)
    if not time_hhmm:
        raise ValueError("时间格式应为 HH:MM。")
    days = _norm_days(days)
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO resource_schedules
            (tenant_name, instance_id, instance_name, action, time_hhmm, days, enabled, last_run_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, '', ?)
            """,
            (tenant_name, instance_id, instance_name, action, time_hhmm, days, now_iso()),
        )
        return int(cursor.lastrowid or 0)


def remove_schedule(tenant_name: str, schedule_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM resource_schedules WHERE id = ? AND tenant_name = ?",
            (schedule_id, tenant_name),
        )


def toggle_schedule(tenant_name: str, schedule_id: int, enabled: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE resource_schedules SET enabled = ? WHERE id = ? AND tenant_name = ?",
            (1 if enabled else 0, schedule_id, tenant_name),
        )


def _matches_today(days: str, weekday: int) -> bool:
    if days == "all":
        return True
    nums = [int(x) for x in days.split(",") if x.isdigit()]
    # Python weekday(): 周一=0..周日=6；我们存的是 周一=1..周日=7
    return (weekday + 1) in nums


def run_due_schedules(now: datetime | None = None) -> list[dict[str, Any]]:
    """执行所有到点的计划。scheduler 每分钟调用。

    last_run_date 防止同一计划在同一天重复执行；
    时间窗口取当前分钟（tick 间隔 30 秒，每分钟最多命中一次）。
    """
    from oci_helpers import find_tenant_config, get_compute_client
    from settings import INSTANCE_ACTIONS

    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    weekday = now.weekday()

    with connect() as conn:
        due = conn.execute(
            "SELECT * FROM resource_schedules WHERE enabled = 1 AND time_hhmm = ? AND last_run_date != ?",
            (hhmm, today),
        ).fetchall()

    executed: list[dict[str, Any]] = []
    for row in due:
        row = dict(row)
        if not _matches_today(row["days"], weekday):
            continue
        # 先标记，避免执行失败后反复重试
        with connect() as conn:
            conn.execute(
                "UPDATE resource_schedules SET last_run_date = ? WHERE id = ?",
                (today, row["id"]),
            )
        tenant_cfg = find_tenant_config(row["tenant_name"])
        if tenant_cfg is None:
            logger.warning("定时计划 %s：租户 %s 不存在，已跳过", row["id"], row["tenant_name"])
            continue
        action_label = _ACTION_LABELS.get(row["action"], row["action"])
        try:
            client = get_compute_client(tenant_cfg)
            instance = client.get_instance(row["instance_id"]).data
            state = getattr(instance, "lifecycle_state", "")
            # 已经是目标状态就别再操作：OCI 会对重复动作返回 409，
            # 否则每天都会收到一条"执行失败"通知
            already = (row["action"] == "start" and state == "RUNNING") or (
                row["action"] == "stop" and state == "STOPPED"
            )
            if already:
                logger.info("定时计划 %s：%s 已处于 %s，跳过", row["id"], row["instance_name"], state)
                continue
            client.instance_action(row["instance_id"], INSTANCE_ACTIONS[row["action"]])
            executed.append({**row, "result": "ok"})
            notify(
                "定时任务已执行",
                f"[{row['tenant_name']}] {row['instance_name']} {action_label}成功。",
            )
        except Exception as exc:
            logger.error("定时计划 %s 执行失败: %s", row["id"], exc)
            executed.append({**row, "result": str(exc)})
            notify(
                "定时任务执行失败",
                f"[{row['tenant_name']}] {row['instance_name']} {action_label}失败: {exc}",
            )
    return executed


def instance_has_schedule(tenant_name: str, instance_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM resource_schedules WHERE tenant_name = ? AND instance_id = ? AND enabled = 1 LIMIT 1",
            (tenant_name, instance_id),
        ).fetchone()
    return row is not None
