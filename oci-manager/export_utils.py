from __future__ import annotations

import csv
from io import BytesIO, StringIO
from typing import Any

from flask import send_file


def send_csv(filename: str, headers: list[str], rows: list[dict[str, Any]]):
    text_buffer = StringIO()
    writer = csv.DictWriter(text_buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in headers})
    payload = text_buffer.getvalue().encode("utf-8-sig")
    return send_file(BytesIO(payload), as_attachment=True, download_name=filename, mimetype="text/csv; charset=utf-8")
