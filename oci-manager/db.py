from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator

from settings import SQLITE_PATH, login_lock_seconds, login_max_failures, login_window_seconds


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
        conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '2')")


def record_login_attempt(remote_addr: str, username: str, success: bool) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO login_attempts (time, remote_addr, username, success) VALUES (?, ?, ?, ?)",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z", remote_addr or "-", username or "-", 1 if success else 0),
        )


def login_is_locked(remote_addr: str) -> bool:
    since = datetime.utcnow() - timedelta(seconds=max(login_window_seconds(), login_lock_seconds()))
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
    cutoff = datetime.utcnow() - timedelta(seconds=max(login_lock_seconds(), login_window_seconds()) * 2)
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


init_db()
cleanup_runtime_rows()
