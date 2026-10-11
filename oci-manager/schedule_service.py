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
from timeout_utils import TimedCallError, run_with_timeout

logger = logging.getLogger("scheduler")

_DAY_LABELS = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
_ACTION_LABELS = {"start": "启动", "stop": "停止"}
# 定时 stop 用 SOFTSTOP（优雅关机）而不是 STOP（强制断电）：
# 每天定时硬断电有文件系统损坏/数据丢失风险；挂死的实例请到实例页手动强停。
_ACTION_OCI = {"start": "START", "stop": "SOFTSTOP"}
# 每条计划的等待上限。没有它，一个卡住的 OCI 调用会让整条调度线程永久阻塞。
_SCHEDULE_TIMEOUT_SECONDS = 30


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
        hour, minute = int(parts[0]), int(parts[1])
        # "99:99" 这类值归一化后永远匹配不上 %H:%M，计划会静默永不执行
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
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
    """执行到点（含错过补跑）的计划。scheduler 每分钟调用。

    窗口匹配：查「今天还没跑、时间 <= 当前」的计划按时间升序补跑——服务停机、
    桌面睡眠或长巡检挤占 tick 跨过计划时间后，下一次 tick 会补上，而不是当天
    静默丢失（旧实现精确匹配当前分钟，错过即丢）。

    last_run_date 在执行后写入：执行后、写标记前崩溃最多重跑一次，而
    _execute_schedule 对已处于目标状态的实例会跳过，重跑无副作用；反过来
    「先标记后执行」在标记与执行之间崩溃会静默漏跑一整天。执行失败也写标记：
    失败会发通知，逐分钟重试只会变成通知轰炸。
    """
    from oci_helpers import find_tenant_config, friendly_error

    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    weekday = now.weekday()

    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM resource_schedules WHERE enabled = 1 AND last_run_date != ? ORDER BY time_hhmm",
            (today,),
        ).fetchall()

    executed: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        if row["time_hhmm"] > hhmm:
            break  # 已按时间升序，后面的还没到点
        if not _matches_today(row["days"], weekday):
            continue
        tenant_cfg = find_tenant_config(row["tenant_name"])
        if tenant_cfg is None:
            logger.warning("定时计划 %s：租户 %s 不存在，标记跳过", row["id"], row["tenant_name"])
            _mark_run(row["id"], today)
            continue
        action_label = _ACTION_LABELS.get(row["action"], row["action"])
        try:
            outcome = run_with_timeout(_SCHEDULE_TIMEOUT_SECONDS, _execute_schedule, tenant_cfg, row)
        except TimedCallError:
            # instance_action 是写操作：超时只代表不再等待，底层调用可能已生效。
            # 不能说"失败"诱导用户重复操作；标记照写，同一天不会再跑。
            logger.error("定时计划 %s 执行超时", row["id"])
            _mark_run(row["id"], today)
            executed.append({**row, "result": "timeout"})
            notify(
                "定时任务结果未知",
                f"[{row['tenant_name']}] {row['instance_name']} {action_label}已提交但超时，结果未知，请到实例页确认。",
            )
            continue
        except Exception as exc:
            logger.error("定时计划 %s 执行失败: %s", row["id"], exc)
            _mark_run(row["id"], today)
            executed.append({**row, "result": str(exc)})
            notify(
                "定时任务执行失败",
                f"[{row['tenant_name']}] {row['instance_name']} {action_label}失败: {friendly_error(exc)}",
            )
            continue
        if outcome == "skipped":
            logger.info("定时计划 %s：%s 已处于目标状态，跳过", row["id"], row["instance_name"])
            _mark_run(row["id"], today)
            continue
        _mark_run(row["id"], today)
        executed.append({**row, "result": "ok"})
        notify(
            "定时任务已执行",
            f"[{row['tenant_name']}] {row['instance_name']} {action_label}成功。",
        )
    return executed


def _mark_run(schedule_id: int, today: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE resource_schedules SET last_run_date = ? WHERE id = ?",
            (today, schedule_id),
        )


def _execute_schedule(tenant_cfg: dict[str, Any], row: dict[str, Any]) -> str:
    """在后台线程里执行单条计划，返回 ok / skipped。

    会被 run_with_timeout 包裹：instance_action 是写操作，超时只代表"不再等待"，
    底层调用仍可能随后完成。这里不会造成重复执行——调用方在开跑前就已经把
    last_run_date 写成今天，同一天不会再命中这条计划。
    """
    from oci_helpers import get_compute_client

    client = get_compute_client(tenant_cfg)
    instance = client.get_instance(row["instance_id"]).data
    state = getattr(instance, "lifecycle_state", "")
    # 已经是目标状态（或正在往目标状态过渡）就别再操作：
    # OCI 会对重复动作返回 409，否则每天都会收到一条"执行失败"通知
    already = (row["action"] == "start" and state in {"RUNNING", "STARTING"}) or (
        row["action"] == "stop" and state in {"STOPPED", "STOPPING"}
    )
    if already:
        return "skipped"
    client.instance_action(row["instance_id"], _ACTION_OCI[row["action"]])
    return "ok"


def disable_schedules_for_instance(tenant_name: str, instance_id: str) -> int:
    """禁用某实例的全部启用中计划（终止实例后调用），返回禁用条数。"""
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE resource_schedules SET enabled = 0 WHERE tenant_name = ? AND instance_id = ? AND enabled = 1",
            (tenant_name, instance_id),
        )
        return int(cursor.rowcount or 0)


def instance_has_schedule(tenant_name: str, instance_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM resource_schedules WHERE tenant_name = ? AND instance_id = ? AND enabled = 1 LIMIT 1",
            (tenant_name, instance_id),
        ).fetchone()
    return row is not None
