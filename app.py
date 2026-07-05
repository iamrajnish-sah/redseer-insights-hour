"""
app.py

FastAPI backend for the news dashboard. Wraps the existing pipeline modules
(epub_parser, rss_ingest, newsapi_ingest, classify_and_summarize, dedupe,
database) behind a small JSON API, and serves the frontend.

Run:
    export GEMINIAPIKEY=...
    export NEWSAPIKEY=...     # optional
    uvicorn app:app --reload --port 8000

Then open http://localhost:8000
"""

import os
import json
import shutil
import smtplib
import tempfile
from datetime import date

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import database
import newspaper_parser
import rss_ingest
import newsapi_ingest
import classify_and_summarize
import dedupe
import email_digest
import admin_auth

from sector_keywords import SECTOR_LABELS, normalize_sector_tags

app = FastAPI(title="Redseer Insight Hour")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


@app.on_event("startup")
def on_startup():
    database.init_db()


def _row_to_dict(row):
    d = dict(row)
    d["sectors"] = normalize_sector_tags(json.loads(d["sectors"] or "[]"))
    return d


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/sectors")
def get_sectors():
    return SECTOR_LABELS


@app.get("/api/dates")
def get_dates():
    return database.get_pub_dates()


@app.get("/api/keywords")
def get_keywords():
    from sector_keywords import SECTOR_KEYWORDS, INDIRECT_KEYWORDS, NEWSAPI_QUERIES, SECTOR_LABELS
    return {
        "sector_labels": SECTOR_LABELS,
        "sector_keywords": SECTOR_KEYWORDS,
        "indirect_keywords": INDIRECT_KEYWORDS,
        "newsapi_queries": NEWSAPI_QUERIES,
    }


@app.get("/api/stats")
def get_stats(_: None = Depends(admin_auth.require_admin)):
    return database.get_stats()


@app.get("/api/email-status")
def email_status(_: None = Depends(admin_auth.require_admin)):
    recipients = email_digest.load_recipients()
    return {
        "smtp_configured": email_digest.smtp_configured(),
        "product_name": email_digest.PRODUCT_NAME,
        "sectors_with_recipients": {
            SECTOR_LABELS.get(k, k): len(v) for k, v in recipients.items() if v
        },
    }


@app.get("/api/articles")
def get_articles(sector: str = None, pub_date: str = None, search: str = None):
    rows = database.get_relevant_articles(pub_date=pub_date, sector=sector, search=search)
    articles = [_row_to_dict(r) for r in rows]
    return articles


@app.get("/api/admin/status")
def admin_status():
    return {"protected": admin_auth.admin_password_configured()}


@app.post("/api/admin/verify")
def admin_verify(x_admin_password: str = Header(default=None, alias="X-Admin-Password")):
    if not admin_auth.admin_password_configured():
        return {"ok": True, "protected": False}
    if admin_auth.verify_admin_password(x_admin_password):
        return {"ok": True, "protected": True}
    raise HTTPException(status_code=401, detail="Incorrect admin password")


@app.post("/api/upload-newspaper")
@app.post("/api/upload-epub")
async def upload_newspaper(
    file: UploadFile = File(...),
    _: None = Depends(admin_auth.require_admin),
):
    if not file.filename or not newspaper_parser.is_supported_upload(file.filename):
        allowed = ", ".join(sorted(newspaper_parser.SUPPORTED_EXTENSIONS))
        raise HTTPException(400, f"Unsupported file type. Allowed: {allowed}")

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, file.filename)
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        articles, file_format, method = newspaper_parser.parse_newspaper(tmp_path)
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not articles:
        raise HTTPException(400, "No articles found in this file.")

    inserted, new_ids, refreshed_ids = database.insert_articles(articles)
    merged = dedupe.run_all_dedupes()["total_merged"]

    method_labels = {
        "epub_structure": "structured epub pages",
        "pdf_text": "PDF text (page by page)",
        "pdf_text_partial": "partial PDF text",
        "pdf_gemini": "scanned PDF via Gemini",
        "image_gemini": "photo/page via Gemini",
        "txt": "plain text blocks",
    }
    next_step = (
        "Saved and pre-tagged."
        if file_format == "epub"
        else "Click Process with Gemini to classify sectors and summarize."
    )

    return {
        "format": file_format,
        "method": method,
        "parsed": len(articles),
        "inserted": inserted,
        "new_ids": new_ids,
        "refreshed_ids": refreshed_ids,
        "merged_duplicates": merged,
        "message": (
            f"Read {len(articles)} item(s) from {file_format.upper()} "
            f"({method_labels.get(method, method)}). {next_step}"
        ),
    }


@app.post("/api/refresh-rss")
def refresh_rss(_: None = Depends(admin_auth.require_admin)):
    articles, stats = rss_ingest.fetch_all()
    inserted, new_ids, refreshed_ids = database.insert_articles(articles)
    dedupe_stats = dedupe.run_all_dedupes()
    merged = dedupe_stats["total_merged"]
    return {
        "fetched": stats["fetched"],
        "matched": stats["matched"],
        "skipped": stats["skipped"],
        "inserted": inserted,
        "refreshed": len(refreshed_ids),
        "merged_duplicates": merged,
        "new_ids": new_ids,
        "refreshed_ids": refreshed_ids,
        "highlight_ids": new_ids + refreshed_ids,
        "keyword_filter": stats["keyword_filter"],
        "message": (
            "Keyword filter ON — matching RSS saved without Gemini."
            if stats["keyword_filter"]
            else "Keyword filter OFF — use Process with Gemini to classify."
        ),
    }


@app.post("/api/refresh-newsapi")
def refresh_newsapi(_: None = Depends(admin_auth.require_admin)):
    if not os.environ.get("NEWSAPIKEY"):
        raise HTTPException(400, "NEWSAPIKEY is not set on the server")
    articles, stats = newsapi_ingest.fetch_all()
    inserted, new_ids, refreshed_ids = database.insert_articles(articles)
    dedupe_stats = dedupe.run_all_dedupes()
    merged = dedupe_stats["total_merged"]
    return {
        "fetched": stats["fetched"],
        "matched": stats["matched"],
        "skipped": stats["skipped"],
        "inserted": inserted,
        "refreshed": len(refreshed_ids),
        "merged_duplicates": merged,
        "new_ids": new_ids,
        "refreshed_ids": refreshed_ids,
        "highlight_ids": new_ids + refreshed_ids,
        "message": "NewsAPI items pre-tagged by sector — no Gemini needed.",
    }


@app.post("/api/process")
def process_unprocessed(_: None = Depends(admin_auth.require_admin)):
    """Classify/tag/summarize all unprocessed articles via Gemini, then dedupe."""
    if not (os.environ.get("GEMINIAPIKEY") or os.environ.get("GEMINI_API_KEY")):
        raise HTTPException(400, "GEMINIAPIKEY is not set on the server")

    rows = database.get_unprocessed()
    if not rows:
        return {"classified": 0, "relevant": 0, "message": "Nothing to process."}

    process_rows, settings, pending_by_origin, total_pending = classify_and_summarize.get_rows_for_processing()
    if not process_rows:
        return {
            "classified": 0,
            "relevant": 0,
            "message": "Nothing to process within current Gemini limits.",
            "pending_in_scope": total_pending,
            "pending_by_origin": pending_by_origin,
        }

    results, stats = classify_and_summarize.classify_batch(
        process_rows,
        body_chars=settings["body_chars"],
    )
    for row in process_rows:
        r = results.get(row["id"])
        if r:
            database.update_classification(
                row["id"], r["is_relevant"], normalize_sector_tags(r["sectors"]), r["summary"]
            )

    relevant_count = sum(1 for r in results.values() if r["is_relevant"])

    dedupe_stats = dedupe.run_all_dedupes()

    message = None
    if stats["models_used"]:
        message = f"Used models: {', '.join(stats['models_used'])}"
    if stats["batches_failed"]:
        extra = f"{stats['batches_failed']} batch(es) skipped — all models hit quota."
        message = f"{message}. {extra}" if message else extra
    remaining = total_pending - len(process_rows)
    if remaining > 0:
        extra = f"{remaining} still pending — run again tomorrow or raise GEMINI_MAX_ARTICLES."
        message = f"{message}. {extra}" if message else extra

    return {
        "classified": len(results),
        "relevant": relevant_count,
        "models_used": stats["models_used"],
        "batches_failed": stats["batches_failed"],
        "processed_now": len(process_rows),
        "pending_in_scope": total_pending,
        "pending_by_origin": pending_by_origin,
        "message": message,
        "merged_duplicates": dedupe_stats["total_merged"],
    }


@app.post("/api/dedupe-all")
def dedupe_all(_: None = Depends(admin_auth.require_admin)):
    """Remove duplicate stories already stored (same URL or same headline)."""
    result = dedupe.run_all_dedupes()
    return {
        **result,
        "message": f"Removed {result['total_merged']} duplicate(s).",
    }


@app.post("/api/send-email")
def send_email_digest(pub_date: str = None, _: None = Depends(admin_auth.require_admin)):
    """Send each sector's news to that sector's email list for the given date."""
    if not pub_date:
        pub_date = str(date.today())
    try:
        results = email_digest.send_sector_digests(pub_date)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except smtplib.SMTPException as e:
        raise HTTPException(500, f"Email failed: {e}")

    sent = [r for r in results if r["status"] == "sent"]
    skipped = [r for r in results if r["status"] == "skipped"]
    return {
        "pub_date": pub_date,
        "sent_count": len(sent),
        "skipped_count": len(skipped),
        "results": results,
        "message": f"Sent {len(sent)} sector email(s) for {pub_date}.",
    }


@app.post("/api/reconcile-ride-hailing")
def reconcile_ride_hailing(_: None = Depends(admin_auth.require_admin)):
    """Remove wrongly tagged ride-hailing articles and add newly matching ones."""
    result = database.reconcile_ride_hailing_tags()
    return {
        **result,
        "message": (
            f"Ride hailing refilter done: {result['removed']} removed, "
            f"{result['added']} added."
        ),
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
