from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from settings import SQLITE_PATH, login_lock_seconds, login_max_failures, login_window_seconds


def _utc_now() -> datetime:
    """当前 UTC 时间的 naive datetime。

    Python 3.12 起 datetime.utcnow() 已废弃（返回 naive UTC，语义容易混淆），
    这里显式取带时区的 UTC 时间后再去掉 tzinfo，保证与既有数据格式一致。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_stamp() -> str:
    """写进时间列的统一字符串格式。

    这些列是字符串且按字典序比较（见 login_is_locked 的 time >= ?），
    所以必须始终保持 `YYYY-MM-DDTHH:MM:SSZ` 这一种形态，不能混入时区偏移。
    """
    return _utc_now().isoformat(timespec="seconds") + "Z"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(SQLITE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                user TEXT NOT NULL,
                tenant_name TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_time ON audit_events(time DESC)")
        # 审计查询与保留清理都是 ORDER BY time DESC, id DESC，复合索引才完整覆盖
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_time_id ON audit_events(time DESC, id DESC)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS launch_tasks (
                id TEXT PRIMARY KEY,
                tenant_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                task_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_launch_tasks_tenant_time ON launch_tasks(tenant_name, created_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT NOT NULL,
                remote_addr TEXT NOT NULL,
                username TEXT NOT NULL,
                success INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_addr_time ON login_attempts(remote_addr, time DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_name TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                instance_name TEXT NOT NULL,
                action TEXT NOT NULL,
                time_hhmm TEXT NOT NULL,
                days TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cost_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                tenant_name TEXT NOT NULL,
                items_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(date, tenant_name)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_snapshots_tenant_date ON cost_snapshots(tenant_name, date)")
        conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '4')")


def record_login_attempt(remote_addr: str, username: str, success: bool) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO login_attempts (time, remote_addr, username, success) VALUES (?, ?, ?, ?)",
            (_utc_stamp(), remote_addr or "-", username or "-", 1 if success else 0),
        )


def login_is_locked(remote_addr: str) -> bool:
    since = _utc_now() - timedelta(seconds=max(login_window_seconds(), login_lock_seconds()))
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS failures
            FROM login_attempts
            WHERE remote_addr = ? AND success = 0 AND time >= ?
            """,
            (remote_addr or "-", since.isoformat(timespec="seconds") + "Z"),
        ).fetchone()
    return int(row["failures"] if row else 0) >= login_max_failures()


def cleanup_runtime_rows() -> None:
    cutoff = _utc_now() - timedelta(seconds=max(login_lock_seconds(), login_window_seconds()) * 2)
    with connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE time < ?", (cutoff.isoformat(timespec="seconds") + "Z",))
        conn.execute(
            """
            DELETE FROM audit_events
            WHERE id NOT IN (SELECT id FROM audit_events ORDER BY time DESC, id DESC LIMIT 10000)
            """
        )
        conn.execute(
            """
            DELETE FROM launch_tasks
            WHERE id NOT IN (SELECT id FROM launch_tasks ORDER BY created_at DESC LIMIT 500)
            """
        )



def bootstrap() -> None:
    """建表并清理过期运行数据。由 app 启动时显式调用一次。

    原先写在模块末尾，import db 就会触发磁盘 IO 和一次全表清理，
    任何只想读两个函数的脚本都会被拖累。
    """
    init_db()
    cleanup_runtime_rows()
