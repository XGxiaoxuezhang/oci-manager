from __future__ import annotations

from flask import Blueprint, flash, redirect, url_for

from check_service import load_checks, run_checks
from rendering import render_page

check_bp = Blueprint("check", __name__)


@check_bp.route("/checks")
def checks_home():
    return render_page("checks", results=load_checks())


@check_bp.route("/checks/run", methods=["POST"])
def checks_run():
    results = run_checks()
    flash("巡检完成。", "success")
    return redirect(url_for("check.checks_home"))
