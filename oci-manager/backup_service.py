from __future__ import annotations

import sqlite3
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from settings import BASE_DIR, DATA_DIR, SQLITE_PATH

SKIP_NAMES = {"backup.zip", "app.db", "app.db-wal", "app.db-shm"}
SOURCE_DIR_RUNTIME_NAMES = {"auth.yaml", "tenants.yaml", "audit.log.jsonl", "launch_tasks.json", "checks.json", "settings.yaml"}
# 恢复时一律拒绝的凭据/关键配置：ZIP 里塞入伪造的 auth.yaml 可直接改掉管理员口令，
# 伪造 .secret_key 可伪造任意会话 cookie，伪造 tenants.yaml 可把私钥路径指到任意位置。
# 这些文件要恢复只能手动替换，不进自动恢复流程。
RESTORE_PROTECTED_NAMES = {"auth.yaml", ".secret_key", "tenants.yaml", "settings.yaml"}
MAX_RESTORE_FILE_SIZE = 100 * 1024 * 1024
MAX_RESTORE_TOTAL_SIZE = 512 * 1024 * 1024
_RESTORE_CHUNK = 64 * 1024


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


def restore_backup_zip(payload: bytes) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    skipped_protected: list[str] = []
    total_size = 0
    try:
        archive = ZipFile(BytesIO(payload), "r")
    except BadZipFile as exc:
        raise ValueError("备份文件不是有效 ZIP。") from exc
    with archive:
        for member in archive.infolist():
            if Path(member.filename).name in {"app.db-wal", "app.db-shm"}:
                continue
            # 先用 ZipInfo 里的声明大小做一次快速预检，真正的防线是下面的解压计数
            if member.file_size > MAX_RESTORE_FILE_SIZE:
                raise ValueError(f"文件过大，已拒绝恢复: {member.filename}")
            target = (DATA_DIR / member.filename).resolve()
            try:
                target.relative_to(DATA_DIR)
            except ValueError:
                raise ValueError(f"备份含越界路径，已中止恢复: {member.filename}") from None
            # 按 resolve 后的最终落点拦截，防止 "tenants/../../auth.yaml" 这类拼写绕过文件名检查
            if target.name in RESTORE_PROTECTED_NAMES and target.parent == DATA_DIR:
                skipped_protected.append(member.filename)
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            # 流式解压：zip 元数据里的大小不可信，必须按真实写出的字节计数，
            # 否则高压缩比的包（zip bomb）能一次性撑爆内存和磁盘。
            with archive.open(member) as src, target.open("wb") as dst:
                while True:
                    chunk = src.read(_RESTORE_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    total_size += len(chunk)
                    if written > MAX_RESTORE_FILE_SIZE or total_size > MAX_RESTORE_TOTAL_SIZE:
                        dst.close()
                        target.unlink(missing_ok=True)
                        raise ValueError("备份解压后超过大小限制，已中止恢复。")
                    dst.write(chunk)
            restored.append(member.filename)
    return restored, skipped_protected


def backup_file_count() -> int:
    total = 1 if SQLITE_PATH.exists() else 0
    return total + sum(1 for path in Path(DATA_DIR).rglob("*") if should_include_backup_path(path))
