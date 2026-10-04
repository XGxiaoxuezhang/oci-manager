from __future__ import annotations

import csv
from io import BytesIO, StringIO
from typing import Any

from flask import send_file

# OWASP CSV 注入缓解：这些前缀会被 Excel/WPS 当公式执行，统一加单引号前缀中和。
# 只处理字符串；数值列（int/float）不受影响。
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _neutralize_formula(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def send_csv(filename: str, headers: list[str], rows: list[dict[str, Any]]):
    text_buffer = StringIO()
    writer = csv.DictWriter(text_buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _neutralize_formula(row.get(key, "")) for key in headers})
    payload = text_buffer.getvalue().encode("utf-8-sig")
    return send_file(BytesIO(payload), as_attachment=True, download_name=filename, mimetype="text/csv; charset=utf-8")
