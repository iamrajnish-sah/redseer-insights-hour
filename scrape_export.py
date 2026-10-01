"""
Excel export for Instagram scrape results.

Default column layout is ready now. When you share your exact template,
set INSTAGRAM_EXCEL_COLUMNS (comma-separated header names mapped below)
or we will remap these fields to match your sheet.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime

# Logical fields we can export. Keys are stable; headers are user-facing.
DEFAULT_INSTAGRAM_COLUMNS = [
    ("handle", "Handle / Source"),
    ("post_url", "Post URL"),
    ("caption", "Caption"),
    ("posted_date", "Posted Date"),
    ("summary", "Summary"),
    ("image_url", "Image URL"),
    ("sectors", "Sectors"),
    ("scraped_at", "Scraped At"),
    ("article_id", "Article ID"),
]


def _row_value(article, key):
    source = article.get("source") or ""
    handle = source.lstrip("@") if source.startswith("@") else source
    if key == "handle":
        return handle
    if key == "post_url":
        return article.get("resolved_url") or article.get("url") or ""
    if key == "caption":
        return article.get("body") or article.get("title") or ""
    if key == "posted_date":
        return article.get("pub_date") or ""
    if key == "summary":
        return article.get("summary") or article.get("title") or ""
    if key == "image_url":
        return article.get("image_url") or ""
    if key == "sectors":
        sectors = article.get("sectors") or []
        if isinstance(sectors, str):
            try:
                sectors = json.loads(sectors)
            except json.JSONDecodeError:
                sectors = [sectors]
        return ", ".join(str(s) for s in sectors)
    if key == "scraped_at":
        return article.get("fetched_at") or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    if key == "article_id":
        return article.get("id") or ""
    return ""


def _columns():
    """Allow override: INSTAGRAM_EXCEL_COLUMNS=handle:Brand,post_url:Link,caption:Text"""
    raw = (os.environ.get("INSTAGRAM_EXCEL_COLUMNS") or "").strip()
    if not raw:
        return list(DEFAULT_INSTAGRAM_COLUMNS)
    cols = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            key, header = part.split(":", 1)
            cols.append((key.strip(), header.strip()))
        else:
            # header-only → match default key by header or key name
            key = part
            header = dict(DEFAULT_INSTAGRAM_COLUMNS).get(key, key)
            cols.append((key, header))
    return cols or list(DEFAULT_INSTAGRAM_COLUMNS)


def instagram_articles_to_xlsx_bytes(articles):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError as exc:
        raise RuntimeError(
            "openpyxl is not installed. Add it to requirements and redeploy."
        ) from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "Instagram Scrape"
    columns = _columns()
    headers = [header for _, header in columns]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for article in articles:
        ws.append([_row_value(article, key) for key, _ in columns])

    # Reasonable column widths
    for idx, header in enumerate(headers, start=1):
        width = 18
        if "URL" in header or "Caption" in header or "Summary" in header:
            width = 42
        ws.column_dimensions[ws.cell(1, idx).column_letter].width = width

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
