from __future__ import annotations

from io import BytesIO

from flask import Blueprint, flash, redirect, request, send_file, url_for

from backup_service import backup_file_count, create_backup_zip, restore_backup_zip
from rendering import render_page
from storage import now_iso

backup_bp = Blueprint("backup", __name__)


@backup_bp.route("/backup")
def backup_home():
    return render_page("backup", file_count=backup_file_count())


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
        restored = restore_backup_zip(backup_file.read())
        flash(f"已恢复 {len(restored)} 个文件。请重启服务。", "success")
    except Exception as exc:
        flash(f"恢复失败：{exc}", "error")
    return redirect(url_for("backup.backup_home"))
