from __future__ import annotations

import sqlite3
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from settings import BASE_DIR, DATA_DIR, SQLITE_PATH

SKIP_NAMES = {"backup.zip", "app.db", "app.db-wal", "app.db-shm"}
SOURCE_DIR_RUNTIME_NAMES = {"auth.yaml", "tenants.yaml", "audit.log.jsonl", "launch_tasks.json", "checks.json", "settings.yaml"}
MAX_RESTORE_FILE_SIZE = 100 * 1024 * 1024
MAX_RESTORE_TOTAL_SIZE = 512 * 1024 * 1024


def sqlite_backup_bytes() -> bytes | None:
    if not SQLITE_PATH.exists():
        return None
    source = sqlite3.connect(SQLITE_PATH)
    target = sqlite3.connect(":memory:")
    try:
        source.backup(target)
        return target.serialize()
    except Exception:
        return SQLITE_PATH.read_bytes()
    finally:
        target.close()
        source.close()


def create_backup_zip() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        db_payload = sqlite_backup_bytes()
        if db_payload is not None:
            archive.writestr("app.db", db_payload)
        for path in DATA_DIR.rglob("*"):
            if not should_include_backup_path(path):
                continue
            archive.write(path, path.relative_to(DATA_DIR).as_posix())
    return buffer.getvalue()


def should_include_backup_path(path: Path) -> bool:
    if not path.is_file() or path.name in SKIP_NAMES:
        return False
    if DATA_DIR != BASE_DIR:
        return True
    relative = path.relative_to(DATA_DIR)
    return relative.parts[0] == "tenants" or path.name in SOURCE_DIR_RUNTIME_NAMES


def restore_backup_zip(payload: bytes) -> list[str]:
    restored: list[str] = []
    total_size = 0
    try:
        archive = ZipFile(BytesIO(payload), "r")
    except BadZipFile as exc:
        raise ValueError("备份文件不是有效 ZIP。") from exc
    with archive:
        for member in archive.infolist():
            if Path(member.filename).name in {"app.db-wal", "app.db-shm"}:
                continue
            if member.file_size > MAX_RESTORE_FILE_SIZE:
                raise ValueError(f"文件过大，已拒绝恢复: {member.filename}")
            total_size += member.file_size
            if total_size > MAX_RESTORE_TOTAL_SIZE:
                raise ValueError("备份包总大小超过限制。")
            target = (DATA_DIR / member.filename).resolve()
            try:
                target.relative_to(DATA_DIR)
            except ValueError:
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, target.open("wb") as dst:
                dst.write(src.read())
            restored.append(member.filename)
    return restored


def backup_file_count() -> int:
    total = 1 if SQLITE_PATH.exists() else 0
    return total + sum(1 for path in Path(DATA_DIR).rglob("*") if should_include_backup_path(path))
