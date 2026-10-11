from __future__ import annotations

from io import BytesIO

from typing import Any

from flask import Blueprint, flash, redirect, request, send_file, url_for

from backup_service import backup_file_count, create_backup_zip, restore_backup_zip, run_scheduled_backup
from rendering import render_page
from settings import DATA_DIR
from storage import now_iso

backup_bp = Blueprint("backup", __name__)


def _auto_backups() -> list[dict[str, Any]]:
    backup_dir = DATA_DIR / "backups"
    rows = []
    for path in sorted(backup_dir.glob("auto-*.zip"), reverse=True):
        stat = path.stat()
        rows.append({"name": path.name, "size": stat.st_size, "created": now_iso()[:10]})
    return rows


@backup_bp.route("/backup")
def backup_home():
    return render_page("backup", file_count=backup_file_count(), auto_backups=_auto_backups())


@backup_bp.route("/backup/auto-run", methods=["POST"])
def backup_auto_run():
    try:
        result = run_scheduled_backup()
        flash(f"自动备份已完成：{Path(result['path']).name}（{result['size'] | filesizeformat}）。", "success")
    except Exception as exc:
        flash(f"自动备份失败: {exc}", "error")
    return redirect(url_for("backup.backup_home"))


@backup_bp.route("/backup/auto-restore", methods=["POST"])
def backup_auto_restore():
    name = Path(request.form.get("name", "")).name
    import re as _re

    if not _re.fullmatch(r"auto-\d{8}\.zip", name):
        flash("备份文件名不合法。", "error")
        return redirect(url_for("backup.backup_home"))
    path = DATA_DIR / "backups" / name
    if not path.exists():
        flash("备份文件不存在。", "error")
        return redirect(url_for("backup.backup_home"))
    try:
        restored, skipped = restore_backup_zip(path.read_bytes())
        message = f"已恢复 {len(restored)} 个文件（{name}）。请重启服务。"
        if skipped:
            message += f" 已跳过敏感配置文件（{', '.join(sorted(set(skipped)))}）。"
        flash(message, "success")
    except Exception as exc:
        flash(f"恢复失败：{exc}", "error")
    return redirect(url_for("backup.backup_home"))


@backup_bp.route("/backup/export")
def backup_export():
    payload = create_backup_zip()
    filename = f"oci-manager-backup-{now_iso().replace(':', '').replace('-', '')}.zip"
    return send_file(BytesIO(payload), as_attachment=True, download_name=filename, mimetype="application/zip")


@backup_bp.route("/backup/restore", methods=["POST"])
def backup_restore():
    backup_file = request.files.get("backup_file")
    if not backup_file or not backup_file.filename:
        flash("请选择备份 ZIP 文件。", "error")
        return redirect(url_for("backup.backup_home"))
    try:
        restored, skipped = restore_backup_zip(backup_file.read())
        message = f"已恢复 {len(restored)} 个文件。请重启服务。"
        if skipped:
            message += f" 已跳过敏感配置文件（{', '.join(sorted(set(skipped)))}），如需恢复请手动替换。"
        flash(message, "success")
    except Exception as exc:
        flash(f"恢复失败：{exc}", "error")
    return redirect(url_for("backup.backup_home"))
