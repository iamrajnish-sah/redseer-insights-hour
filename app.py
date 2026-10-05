"""
app.py

FastAPI backend for the news dashboard. Wraps the existing pipeline modules
(epub_parser, rss_ingest, newsapi_ingest, classify_and_summarize, dedupe,
database) behind a small JSON API, and serves the frontend.

Run:
    export GEMINIAPIKEY=...                    # news + newspaper
    export INTELLIGENCE_GEMINI_API_KEY=...     # Intelligence Hub generate
    export NEWSAPIKEY=...                      # optional
    uvicorn app:app --reload --port 8000

Then open http://localhost:8000
"""

import os
import json
import shutil
import smtplib
import tempfile
from datetime import date, datetime, timedelta

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Header, Request, Query
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import database
import db_conn
import newspaper_parser
import rss_ingest
import newsapi_ingest
import gnews_ingest
import classify_and_summarize
import dedupe
import email_digest
import subscribers
import intelligence_hub
import admin_auth
import auto_refresh
import scrape_ingest
import festive_jobs
import report_export
import scrape_export
from db_path import using_persistent_disk
from db_persist import restore_db, save_db, storage_status, enabled

from sector_keywords import (
    SECTOR_LABELS,
    GNEWS_QUERIES,
    GOOGLE_NEWS_QUERIES,
    normalize_sector_tags,
    is_festive_sale_relevant,
)

app = FastAPI(title="Redseer Insight Hour")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


@app.middleware("http")
async def allow_head_requests(request: Request, call_next):
    """Uptime checks send HEAD; FastAPI GET routes otherwise return 405."""
    is_head = request.method == "HEAD"
    if is_head:
        request.scope["method"] = "GET"
    response = await call_next(request)
    if not is_head:
        return response
    body = getattr(response, "body", None)
    if body is None and hasattr(response, "body_iterator"):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        body = b"".join(chunks)
    headers = dict(response.headers)
    headers["content-length"] = str(len(body or b""))
    return Response(status_code=response.status_code, headers=headers)


def _cron_allowed(authorization, user_agent, cron_schedule, admin_password):
    """Allow Vercel Cron, CRON_SECRET bearer, or admin password. Open only in local dev."""
    secret = (os.environ.get("CRON_SECRET") or "").strip()
    if secret and (authorization or "") == f"Bearer {secret}":
        return True
    if admin_auth.admin_password_configured() and admin_auth.verify_admin_password(admin_password):
        return True
    ua = (user_agent or "").lower()
    if ua.startswith("vercel-cron/"):
        return True
    if cron_schedule and os.environ.get("VERCEL"):
        return True
    hosted = any(
        os.environ.get(k)
        for k in ("VERCEL", "RENDER", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID")
    )
    if hosted:
        return False
    return True


# Set during startup if Turso/init fails — site still serves pages.
STARTUP_ERROR = None


@app.on_event("startup")
def on_startup():
    global STARTUP_ERROR
    STARTUP_ERROR = None
    try:
        # Clear any hung refresh lock left by a killed Vercel invocation.
        try:
            database.release_refresh_lock()
        except Exception:
            pass
        # Turso is the durable store — skip Blob restore (Blob free limits already failed).
        if database.using_turso():
            database.init_db(light=True)
            scrape_ingest.init_scrape_tables()
            try:
                seeded = scrape_ingest.seed_default_targets()
                if seeded.get("added"):
                    print(f"[startup] seeded {seeded['added']} default scrape targets (Turso)")
            except Exception as exc:
                print(f"[startup] scrape seed skipped: {exc}")
            print("[startup] using Turso durable database")
            return

        problem = db_conn.turso_config_problem()
        if problem:
            STARTUP_ERROR = problem
            print(f"[startup] Turso not ready: {problem}")

        # Persistent disk (Render/Railway) keeps SQLite across restarts — skip Blob restore.
        if not using_persistent_disk():
            restore_db(database.DB_PATH)
        database.init_db(light=False)
        scrape_ingest.init_scrape_tables()
        try:
            seeded = scrape_ingest.seed_default_targets()
            if seeded.get("added"):
                print(f"[startup] seeded {seeded['added']} default scrape targets")
        except Exception as exc:
            print(f"[startup] scrape seed skipped: {exc}")
        if not using_persistent_disk():
            restore_db(database.DB_PATH)
    except Exception as exc:
        # Never brick the whole website because DB bootstrap failed.
        STARTUP_ERROR = str(exc)
        print(f"[startup] FAILED (site will still boot): {exc}")


@app.get("/api/health")
def health():
    """Public health — use this to debug Turso / env issues without admin login."""
    info = {
        "ok": STARTUP_ERROR is None and db_conn.LAST_ERROR is None and (
            (not database.using_turso()) or db_conn.LAST_DRIVER in ("http", "native", "embedded")
        ),
        "startup_error": STARTUP_ERROR,
        "db_error": db_conn.LAST_ERROR,
        "db_driver": db_conn.LAST_DRIVER,
        "turso_configured": database.using_turso(),
        "turso_problem": db_conn.turso_config_problem(),
        "turso_http_url": db_conn.turso_http_url() if db_conn.turso_url() else None,
        "storage": database.storage_info(),
    }
    if database.using_turso():
        if db_conn.LAST_ERROR:
            info["ok"] = False
            info["turso_ping"] = db_conn.LAST_ERROR
        else:
            try:
                with database.get_conn() as conn:
                    row = conn.execute("SELECT COUNT(*) AS c FROM articles").fetchone()
                    info["article_count"] = row["c"] if row else 0
                    info["active_backend"] = getattr(conn, "backend", None)
                info["turso_ping"] = "ok" if db_conn.LAST_DRIVER in ("http", "native", "embedded") else "fallback-local"
                if info["turso_ping"] != "ok":
                    info["ok"] = False
            except Exception as exc:
                info["ok"] = False
                info["turso_ping"] = str(exc)
    return info


def _row_to_dict(row):
    d = dict(row)
    d["sectors"] = normalize_sector_tags(json.loads(d["sectors"] or "[]"))
    source = d.get("source") or ""
    origin = d.get("origin") or ""
    if origin == "google_news" or source.startswith("Google News"):
        from rss_ingest import sanitize_google_news_image

        d["image_url"] = sanitize_google_news_image(d.get("image_url"))
    raw_meta = d.get("scrape_meta")
    if raw_meta and isinstance(raw_meta, str):
        try:
            d["scrape_meta"] = json.loads(raw_meta)
        except json.JSONDecodeError:
            d["scrape_meta"] = {}
    elif not raw_meta:
        d["scrape_meta"] = {}
    return d


@app.get("/")
def serve_index():
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "X-App-View": "intel-hub-collapse",
        },
    )


@app.get("/preferences")
def serve_preferences():
    return FileResponse(
        os.path.join(STATIC_DIR, "preferences.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/admin/subscribers")
def serve_subscribers_admin():
    return FileResponse(
        os.path.join(STATIC_DIR, "subscribers-admin.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/admin/intelligence")
def serve_intelligence_hub():
    return FileResponse(
        os.path.join(STATIC_DIR, "intelligence-hub.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/intelligence")
def serve_public_intelligence_hub():
    return FileResponse(
        os.path.join(STATIC_DIR, "intelligence-hub.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


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
    summary = email_digest.email_recipient_summary()
    return {
        "smtp_configured": email_digest.smtp_configured(),
        "product_name": email_digest.PRODUCT_NAME,
        "subscriber_mode": summary["subscriber_mode"],
        "active_subscribers": summary["active_subscribers"],
        "sectors_with_subscribers": summary["sectors_with_subscribers"],
        "legacy_sectors_with_recipients": summary["legacy_sectors_with_recipients"],
        "sectors_with_recipients": (
            summary["sectors_with_subscribers"]
            if summary["subscriber_mode"]
            else summary["legacy_sectors_with_recipients"]
        ),
    }


@app.post("/api/subscribe")
async def api_subscribe(request: Request):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON body") from exc
    try:
        subscriber, created = subscribers.subscribe(
            name=body.get("name"),
            email=body.get("email"),
            company=body.get("company"),
            designation=body.get("designation"),
            sectors=body.get("sectors") or [],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "created": created,
        "message": (
            "You're subscribed! You'll receive sector news by email."
            if created
            else "Your subscription has been updated."
        ),
        "subscriber": {
            "id": subscriber["id"],
            "name": subscriber["name"],
            "email": subscriber["email"],
            "sectors": subscriber["sectors"],
            "status": subscriber["status"],
        },
    }


@app.get("/api/subscribers/by-token/{token}")
def api_subscriber_by_token(token: str):
    subscriber = subscribers.get_subscriber_by_token(token)
    if not subscriber:
        raise HTTPException(404, "Invalid or expired link")
    return {
        "id": subscriber["id"],
        "name": subscriber["name"],
        "email": subscriber["email"],
        "company": subscriber.get("company"),
        "designation": subscriber.get("designation"),
        "sectors": subscriber["sectors"],
        "status": subscriber["status"],
    }


@app.put("/api/subscribers/preferences")
async def api_update_preferences(request: Request):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON body") from exc
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "Token is required")
    try:
        subscriber = subscribers.update_preferences_by_token(token, body.get("sectors") or [])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "message": "Your preferences have been updated.",
        "subscriber": {
            "id": subscriber["id"],
            "name": subscriber["name"],
            "email": subscriber["email"],
            "sectors": subscriber["sectors"],
            "status": subscriber["status"],
        },
    }


@app.get("/api/unsubscribe")
def api_unsubscribe(token: str = Query(...)):
    try:
        subscriber = subscribers.unsubscribe_by_token(token)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unsubscribed — Redseer Insight Hour</title>
<style>
body {{ font-family: Arial, sans-serif; background:#f1f5f9; margin:0; padding:40px 16px; }}
.card {{ max-width:520px; margin:0 auto; background:#fff; border-radius:14px; padding:32px; box-shadow:0 8px 24px rgba(15,23,42,.08); }}
h1 {{ margin:0 0 12px; color:#1e3a5f; font-size:1.5rem; }}
p {{ color:#475569; line-height:1.6; }}
a {{ color:#0d9488; }}
</style></head><body><div class="card">
<h1>You've been unsubscribed</h1>
<p>{subscriber['email']} will no longer receive {email_digest.PRODUCT_NAME} emails.</p>
<p>Changed your mind? <a href="/preferences?token={token}">Manage preferences</a> or <a href="/">return to the dashboard</a>.</p>
</div></body></html>"""
    return HTMLResponse(html)


@app.get("/api/admin/subscribers")
def admin_list_subscribers(
    search: str = None,
    sector: str = None,
    status: str = None,
    _: None = Depends(admin_auth.require_admin),
):
    try:
        rows = subscribers.list_subscribers(search=search, sector=sector, status=status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"subscribers": rows, "count": len(rows)}


@app.get("/api/admin/subscribers/analytics")
def admin_subscriber_analytics(_: None = Depends(admin_auth.require_admin)):
    return subscribers.get_analytics()


@app.get("/api/admin/subscribers/export")
def admin_export_subscribers(
    search: str = None,
    sector: str = None,
    status: str = None,
    _: None = Depends(admin_auth.require_admin),
):
    try:
        csv_data = subscribers.export_csv(search=search, sector=sector, status=status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    filename = f"subscribers-{date.today()}.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/admin/subscribers")
async def admin_create_subscriber(request: Request, _: None = Depends(admin_auth.require_admin)):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON body") from exc
    try:
        subscriber, _ = subscribers.subscribe(
            name=body.get("name"),
            email=body.get("email"),
            company=body.get("company"),
            designation=body.get("designation"),
            sectors=body.get("sectors") or [],
        )
        if body.get("status") == subscribers.STATUS_UNSUBSCRIBED:
            subscriber = subscribers.update_subscriber(
                subscriber["id"], status=subscribers.STATUS_UNSUBSCRIBED
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "subscriber": subscriber}


@app.put("/api/admin/subscribers/{subscriber_id}")
async def admin_update_subscriber(
    subscriber_id: int,
    request: Request,
    _: None = Depends(admin_auth.require_admin),
):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON body") from exc
    try:
        subscriber = subscribers.update_subscriber(
            subscriber_id,
            name=body.get("name"),
            email=body.get("email"),
            company=body.get("company"),
            designation=body.get("designation"),
            status=body.get("status"),
            sectors=body.get("sectors"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "subscriber": subscriber}


@app.delete("/api/admin/subscribers/{subscriber_id}")
def admin_delete_subscriber(subscriber_id: int, _: None = Depends(admin_auth.require_admin)):
    try:
        subscribers.delete_subscriber(subscriber_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "message": "Subscriber deleted."}


# ── Intelligence Hub (independent module; does not touch news pipeline) ──


@app.get("/api/intelligence/status")
def public_intelligence_status():
    return {
        "configured": intelligence_hub.intelligence_configured(),
        "message": (
            "Read published sector briefs below. Generating a new brief needs the admin password and INTELLIGENCE_GEMINI_API_KEY."
            if intelligence_hub.intelligence_configured()
            else "Intelligence Hub is public for reading. Set INTELLIGENCE_GEMINI_API_KEY in Vercel to generate new briefs."
        ),
        "sectors": SECTOR_LABELS,
        "public": True,
    }


@app.get("/api/intelligence/reports")
def public_list_intelligence_reports(sector: str = None, limit: int = 50):
    try:
        rows = intelligence_hub.list_reports(sector=sector, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    for row in rows:
        row["sector_label"] = SECTOR_LABELS.get(row["sector"], row["sector"])
    return {"reports": rows, "count": len(rows)}


@app.get("/api/intelligence/reports/{report_id}")
def public_get_intelligence_report(report_id: int):
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return intelligence_hub.enrich_report_with_sources(report)


@app.get("/api/intelligence/lookup")
def public_lookup_intelligence(sector: str, start_date: str, end_date: str = None):
    """Anyone can read a processed brief for a sector + date range."""
    try:
        rows = intelligence_hub.reports_for_period(sector, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    reports = [intelligence_hub.enrich_report_with_sources(row) for row in rows]
    return {
        "sector": sector,
        "start_date": start_date,
        "end_date": end_date or start_date,
        "count": len(reports),
        "reports": reports,
    }


@app.get("/api/intelligence/briefs")
def public_intelligence_briefs(limit: int = 12):
    """Latest briefs with executive summaries for the public dashboard."""
    rows = intelligence_hub.list_reports(limit=limit)
    briefs = []
    for row in rows:
        full = intelligence_hub.get_report(row["id"])
        if not full:
            continue
        payload = full.get("report") or {}
        briefs.append({
            "id": row["id"],
            "sector": row["sector"],
            "sector_label": SECTOR_LABELS.get(row["sector"], row["sector"]),
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "period_label": row.get("period_label") or intelligence_hub.period_label(row["start_date"], row["end_date"]),
            "generated_at": row["generated_at"],
            "article_count": row["article_count"],
            "executive_summary": payload.get("executive_summary") or "",
        })
    return briefs


@app.get("/api/intelligence/reports/{report_id}/export.json")
def public_export_intelligence_json(report_id: int):
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    enriched = intelligence_hub.enrich_report_with_sources(report)
    filename = f"intelligence-{report['sector']}-{report['start_date']}-{report['end_date']}.json"
    return JSONResponse(
        enriched,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/intelligence/reports/{report_id}/export.md")
def public_export_intelligence_markdown(report_id: int):
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    filename = f"intelligence-{report['sector']}-{report['start_date']}-{report['end_date']}.md"
    return Response(
        report.get("report_markdown") or "",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/admin/intelligence/status")
def admin_intelligence_status(_: None = Depends(admin_auth.require_admin)):
    return {
        "configured": intelligence_hub.intelligence_configured(),
        "message": (
            "Intelligence Hub ready (separate Gemini key)."
            if intelligence_hub.intelligence_configured()
            else "Set INTELLIGENCE_GEMINI_API_KEY for Intelligence Hub (separate from GEMINIAPIKEY used for news)."
        ),
        "sectors": SECTOR_LABELS,
    }


@app.get("/api/admin/intelligence/preview")
def admin_intelligence_preview(
    sector: str,
    start_date: str,
    end_date: str = None,
    _: None = Depends(admin_auth.require_admin),
):
    try:
        articles = intelligence_hub.fetch_sector_articles(sector, start_date, end_date)
        selected, stats = intelligence_hub.prepare_intelligence_articles(articles)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "sector": sector,
        "start_date": start_date,
        "end_date": end_date or start_date,
        "article_count": stats["source_count"],
        "consolidated_count": stats["consolidated_count"],
        "used_count": stats["used_count"],
        "capped": stats["capped"],
        "article_cap": stats["article_cap"],
        "articles": [
            {
                "id": a["id"],
                "title": a.get("title"),
                "pub_date": a.get("pub_date"),
                "source": a.get("source"),
                "url": a.get("resolved_url") or a.get("url"),
            }
            for a in selected
        ],
    }


@app.get("/api/admin/intelligence/reports")
def admin_list_intelligence_reports(
    sector: str = None,
    limit: int = 50,
    _: None = Depends(admin_auth.require_admin),
):
    try:
        rows = intelligence_hub.list_reports(sector=sector, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    for row in rows:
        row["sector_label"] = SECTOR_LABELS.get(row["sector"], row["sector"])
    return {"reports": rows, "count": len(rows)}


@app.get("/api/admin/intelligence/reports/{report_id}")
def admin_get_intelligence_report(report_id: int, _: None = Depends(admin_auth.require_admin)):
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return intelligence_hub.enrich_report_with_sources(report)


@app.post("/api/admin/intelligence/generate")
async def admin_generate_intelligence(
    request: Request,
    _: None = Depends(admin_auth.require_admin),
):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON body") from exc

    sector = body.get("sector")
    start_date = body.get("start_date") or body.get("date")
    end_date = body.get("end_date") or start_date
    force = bool(body.get("force") or body.get("regenerate"))
    generated_by = (body.get("generated_by") or "admin").strip() or "admin"

    try:
        report = intelligence_hub.generate_or_get_report(
            sector=sector,
            start_date=start_date,
            end_date=end_date,
            force=force,
            generated_by=generated_by,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            500,
            f"Intelligence generation failed: {intelligence_hub.friendly_intelligence_error(exc)}",
        ) from exc

    enriched = intelligence_hub.enrich_report_with_sources(report)
    return {
        "ok": True,
        "cached": bool(report.get("cached")),
        "message": (
            "Returned cached intelligence report."
            if report.get("cached")
            else "Intelligence report generated."
        ),
        "report": enriched,
    }


@app.post("/api/admin/intelligence/email-week")
async def admin_email_weekly_intelligence(
    request: Request,
    _: None = Depends(admin_auth.require_admin),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    start_date = (body or {}).get("start_date")
    end_date = (body or {}).get("end_date")
    try:
        result = email_digest.send_weekly_intelligence_emails(start_date, end_date)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Weekly email failed: {exc}") from exc
    return {"ok": True, **result}


@app.delete("/api/admin/intelligence/reports/{report_id}")
def admin_delete_intelligence_report(report_id: int, _: None = Depends(admin_auth.require_admin)):
    try:
        intelligence_hub.delete_report(report_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "message": "Report deleted."}


@app.get("/api/admin/intelligence/reports/{report_id}/export.json")
def admin_export_intelligence_json(report_id: int, _: None = Depends(admin_auth.require_admin)):
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    enriched = intelligence_hub.enrich_report_with_sources(report)
    filename = f"intelligence-{report['sector']}-{report['start_date']}-{report['end_date']}.json"
    return Response(
        content=json.dumps(enriched, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/admin/intelligence/reports/{report_id}/export.md")
def admin_export_intelligence_markdown(report_id: int, _: None = Depends(admin_auth.require_admin)):
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    filename = f"intelligence-{report['sector']}-{report['start_date']}-{report['end_date']}.md"
    return Response(
        content=report.get("report_markdown") or "",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/admin/intelligence/reports/{report_id}/export.pdf")
@app.get("/api/admin/intelligence/reports/{report_id}/export.html")
def admin_export_intelligence_html(report_id: int, _: None = Depends(admin_auth.require_admin)):
    """Print-ready HTML export (browser Print → Save as PDF)."""
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    enriched = intelligence_hub.enrich_report_with_sources(report)
    html = report_export.report_to_html(enriched)
    return HTMLResponse(html)


@app.get("/api/admin/intelligence/reports/{report_id}/export.docx")
def admin_export_intelligence_docx(report_id: int, _: None = Depends(admin_auth.require_admin)):
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    enriched = intelligence_hub.enrich_report_with_sources(report)
    try:
        data = report_export.report_to_docx_bytes(enriched)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    filename = f"intelligence-{enriched['sector']}-{enriched['start_date']}-{enriched['end_date']}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/intelligence/reports/{report_id}/export.html")
def public_export_intelligence_html(report_id: int):
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    enriched = intelligence_hub.enrich_report_with_sources(report)
    return HTMLResponse(report_export.report_to_html(enriched))


@app.get("/api/intelligence/reports/{report_id}/export.docx")
def public_export_intelligence_docx(report_id: int):
    report = intelligence_hub.get_report(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    enriched = intelligence_hub.enrich_report_with_sources(report)
    try:
        data = report_export.report_to_docx_bytes(enriched)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    filename = f"intelligence-{enriched['sector']}-{enriched['start_date']}-{enriched['end_date']}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/articles")
def get_articles(sector: str = None, pub_date: str = None, search: str = None, days: int = None):
    rows = database.get_relevant_articles(pub_date=pub_date, sector=sector, search=search, days=days)
    articles = [_row_to_dict(r) for r in rows]
    return articles


@app.get("/api/festive-sale")
def get_festive_sale_news(days: int = 30, limit: int = 24):
    """Public viewer strip for the Festive Sale sector."""
    days = max(1, min(int(days or 30), 90))
    limit = max(1, min(int(limit or 24), 60))
    rows = database.get_relevant_articles(sector="festive_sale", days=days)
    articles = [_row_to_dict(row) for row in rows[:limit]]
    if len(articles) < limit:
        # Backfill older rows that match festive keywords but were tagged
        # before festive_sale became a first-class sector.
        seen = {a["id"] for a in articles}
        for row in database.get_relevant_articles(days=days):
            if row["id"] in seen:
                continue
            item = _row_to_dict(row)
            if is_festive_sale_relevant(
                item.get("title") or "",
                item.get("body") or item.get("summary") or "",
                item.get("subtitle") or "",
            ):
                articles.append(item)
                seen.add(item["id"])
            if len(articles) >= limit:
                break
    return {
        "label": "Festive Sale News",
        "days": days,
        "count": len(articles),
        "articles": articles,
    }


@app.get("/api/scrape/website")
def get_website_scrape_news(days: int = 30, limit: int = 36):
    """Dedicated box: news captured from website scrape targets."""
    rows = database.get_scrape_articles("website_scrape", days=days, limit=limit)
    articles = [_row_to_dict(row) for row in rows]
    return {
        "label": "Website scrape news",
        "origin": "website_scrape",
        "days": max(1, min(int(days or 30), 90)),
        "count": len(articles),
        "articles": articles,
    }


@app.get("/api/scrape/instagram")
def get_instagram_scrape_news(days: int = 30, limit: int = 36):
    """Dedicated box: posts captured from Instagram scrape targets."""
    rows = database.get_scrape_articles("instagram", days=days, limit=limit)
    articles = [_row_to_dict(row) for row in rows]
    return {
        "label": "Instagram scrape news",
        "origin": "instagram",
        "days": max(1, min(int(days or 30), 90)),
        "count": len(articles),
        "articles": articles,
        "excel_ready": True,
        "excel_note": (
            "Excel matches the Flipkart IG Posts template (#, Date/Time IST, metrics, "
            "Open post / View image links). Use ?handle=flipkart&start_date=&end_date= on download."
        ),
    }


@app.get("/api/scrape/instagram/export.xlsx")
def export_instagram_scrape_excel(
    days: int = 30,
    limit: int = 500,
    handle: str = None,
    start_date: str = None,
    end_date: str = None,
    label: str = None,
):
    """Excel export of Instagram scrape rows (Flipkart-style workbook)."""
    rows = database.get_scrape_articles(
        "instagram",
        days=days,
        limit=limit,
        start_date=start_date,
        end_date=end_date,
        profile_handle=handle,
    )
    articles = [_row_to_dict(row) for row in rows]
    try:
        data = scrape_export.instagram_articles_to_xlsx_bytes(
            articles, handle=handle, label=label
        )
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    filename = scrape_export.export_filename(handle, start_date, end_date)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/admin/articles/{article_id}")
def admin_delete_article(article_id: int, _: None = Depends(admin_auth.require_admin)):
    """Remove a news card. Admin only. The same story will not be re-ingested."""
    deleted = database.delete_article(article_id)
    if not deleted:
        raise HTTPException(404, "Article not found")
    database.persist()
    return {
        "ok": True,
        **deleted,
        "message": "Article deleted. It will not come back on the next refresh.",
    }


@app.get("/api/feed-summary")
def feed_summary():
    """Public counts + last auto-refresh time (no admin password)."""
    status = auto_refresh.refresh_status()
    return {
        "relevant": status.get("article_count") or 0,
        "total": status.get("total_articles") or status.get("article_count") or 0,
        "last_refresh": status["last_refresh"],
        "stale": status["stale"],
        "refresh_hours": status["refresh_hours"],
        "cron_schedule": status.get("cron_full"),
        "cron_rss": status.get("cron_rss"),
        "sources": status.get("sources") or {},
    }


@app.get("/api/auto-refresh/status")
def auto_refresh_status():
    status = auto_refresh.refresh_status()
    status["turso"] = database.using_turso()
    status["storage"] = database.storage_info()
    status["last_cron"] = {
        "hourly": database.get_meta("last_cron_hourly"),
        "metered": database.get_meta("last_cron_metered"),
        "festive_intelligence": database.get_meta("last_cron_festive_intelligence"),
        "scrape": database.get_meta("last_cron_scrape"),
        "full": database.get_meta("last_cron_full"),
    }
    status["automation_note"] = (
        "Hourly / 3h / 5h jobs are triggered by GitHub Actions using CRON_SECRET — "
        "you do not need to open the website. Data stays in Turso when configured."
        if database.using_turso()
        else (
            "Configure TURSO_DATABASE_URL + TURSO_AUTH_TOKEN so news survives cold starts. "
            "GitHub Actions + CRON_SECRET run refreshes without opening the site."
        )
    )
    return status


def _mark_cron(name):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat() + "Z"
    database.set_meta(f"last_cron_{name}", now)
    return now


@app.post("/api/auto-refresh")
def auto_refresh_now():
    """Public endpoint: refresh feeds when news is empty or older than AUTO_REFRESH_HOURS."""
    try:
        return auto_refresh.try_auto_refresh(force=False)
    except Exception as exc:
        raise HTTPException(500, f"Auto refresh failed: {exc}") from exc


@app.get("/api/cron/refresh")
def cron_refresh(
    authorization: str = Header(default=None, alias="Authorization"),
    user_agent: str = Header(default=None, alias="User-Agent"),
    x_vercel_cron_schedule: str = Header(default=None, alias="X-Vercel-Cron-Schedule"),
    x_admin_password: str = Header(default=None, alias="X-Admin-Password"),
):
    """Vercel Cron / GitHub Actions — full refresh without opening the website."""
    if not _cron_allowed(
        authorization, user_agent, x_vercel_cron_schedule, x_admin_password
    ):
        raise HTTPException(401, "Unauthorized")
    try:
        result = auto_refresh.try_auto_refresh(force=True)
        result["cron_marked_at"] = _mark_cron("full")
        result["turso"] = database.using_turso()
        return result
    except Exception as exc:
        raise HTTPException(500, f"Cron refresh failed: {exc}") from exc


@app.get("/api/cron/refresh-rss")
def cron_refresh_rss(
    authorization: str = Header(default=None, alias="Authorization"),
    user_agent: str = Header(default=None, alias="User-Agent"),
    x_vercel_cron_schedule: str = Header(default=None, alias="X-Vercel-Cron-Schedule"),
    x_admin_password: str = Header(default=None, alias="X-Admin-Password"),
):
    """Second daily cron — RSS only (free, high volume)."""
    if not _cron_allowed(
        authorization, user_agent, x_vercel_cron_schedule, x_admin_password
    ):
        raise HTTPException(401, "Unauthorized")
    try:
        result = auto_refresh.try_auto_refresh(force=True, rss_only=True)
        result["cron_marked_at"] = _mark_cron("rss")
        return result
    except Exception as exc:
        raise HTTPException(500, f"RSS cron failed: {exc}") from exc


@app.get("/api/cron/refresh-hourly")
def cron_refresh_hourly(
    authorization: str = Header(default=None, alias="Authorization"),
    user_agent: str = Header(default=None, alias="User-Agent"),
    x_vercel_cron_schedule: str = Header(default=None, alias="X-Vercel-Cron-Schedule"),
    x_admin_password: str = Header(default=None, alias="X-Admin-Password"),
):
    """Hourly: free RSS + Festive GNews, then Gemini summarize FESTIVE only."""
    if not _cron_allowed(
        authorization, user_agent, x_vercel_cron_schedule, x_admin_password
    ):
        raise HTTPException(401, "Unauthorized")
    try:
        summary = auto_refresh.try_auto_refresh(force=True, rss_only=True)
        retagged = database.reconcile_festive_sale_tags()
        if retagged.get("added"):
            database.persist()
        festive = {"inserted": 0, "refreshed": 0, "skipped": True}
        if os.environ.get("GNEWSAPIKEY") or os.environ.get("GNEWS_API_KEY"):
            try:
                festive = _refresh_one_sector("festive_sale")
            except Exception as exc:
                festive = {"error": str(exc), "inserted": 0, "refreshed": 0}
        gemini = festive_jobs.summarize_festive_only()
        classified = gemini.get("classified", 0)
        if gemini.get("error") or gemini.get("reason") == "no_gemini_key":
            summary = {**(summary or {}), "gemini": gemini}
        marked = _mark_cron("hourly")
        return {
            "ok": True,
            "rss": summary,
            "festive_sale": festive,
            "retagged": retagged,
            "classified": classified,
            "festive_summary_only": True,
            "gemini": gemini,
            "turso": database.using_turso(),
            "cron_marked_at": marked,
            "message": (
                f"Hourly refresh done — festive "
                f"{festive.get('inserted', 0)} new / {festive.get('refreshed', 0)} updated, "
                f"{retagged.get('added', 0)} retagged, {classified} festive summarized "
                f"(other sectors not summarized hourly)."
            ),
        }
    except Exception as exc:
        raise HTTPException(500, f"Hourly cron failed: {exc}") from exc


@app.get("/api/cron/refresh-metered")
def cron_refresh_metered(
    authorization: str = Header(default=None, alias="Authorization"),
    user_agent: str = Header(default=None, alias="User-Agent"),
    x_vercel_cron_schedule: str = Header(default=None, alias="X-Vercel-Cron-Schedule"),
    x_admin_password: str = Header(default=None, alias="X-Admin-Password"),
):
    """Every ~3 hours: NewsAPI + full GNews (metered sources)."""
    if not _cron_allowed(
        authorization, user_agent, x_vercel_cron_schedule, x_admin_password
    ):
        raise HTTPException(401, "Unauthorized")
    try:
        summary = auto_refresh.run_refresh(
            include_rss=False,
            include_gnews=True,
            include_newsapi=True,
        )
        marked = _mark_cron("metered")
        return {
            "ok": True,
            **summary,
            "turso": database.using_turso(),
            "cron_marked_at": marked,
            "message": (
                f"Metered refresh done — inserted {summary.get('total_inserted', 0)}, "
                f"updated {summary.get('total_refreshed', 0)}."
            ),
        }
    except Exception as exc:
        raise HTTPException(500, f"Metered cron failed: {exc}") from exc


@app.get("/api/cron/festive-intelligence")
def cron_festive_intelligence(
    authorization: str = Header(default=None, alias="Authorization"),
    user_agent: str = Header(default=None, alias="User-Agent"),
    x_vercel_cron_schedule: str = Header(default=None, alias="X-Vercel-Cron-Schedule"),
    x_admin_password: str = Header(default=None, alias="X-Admin-Password"),
):
    """Every ~5 hours: website+IG scrape, festive intelligence brief, email."""
    if not _cron_allowed(
        authorization, user_agent, x_vercel_cron_schedule, x_admin_password
    ):
        raise HTTPException(401, "Unauthorized")
    try:
        seeded = scrape_ingest.seed_default_targets()
        # Pull scrape targets first so new site/IG posts enter the brief
        scrape = scrape_ingest.run_all_enabled()
        retagged = database.reconcile_festive_sale_tags()
        festive_jobs.summarize_festive_only()
        result = festive_jobs.run_festive_intelligence_and_email(force=True)
        marked = _mark_cron("festive_intelligence")
        database.set_meta("last_cron_scrape", marked)
        return {
            "ok": True,
            "seeded": seeded,
            "scrape": scrape,
            "retagged": retagged,
            "turso": database.using_turso(),
            "cron_marked_at": marked,
            **result,
            "message": "Festive intelligence cycle finished (no website visit needed).",
        }
    except Exception as exc:
        raise HTTPException(500, f"Festive intelligence cron failed: {exc}") from exc


@app.get("/api/cron/scrape-targets")
def cron_scrape_targets(
    authorization: str = Header(default=None, alias="Authorization"),
    user_agent: str = Header(default=None, alias="User-Agent"),
    x_vercel_cron_schedule: str = Header(default=None, alias="X-Vercel-Cron-Schedule"),
    x_admin_password: str = Header(default=None, alias="X-Admin-Password"),
):
    """Run all enabled website + Instagram scrape targets."""
    if not _cron_allowed(
        authorization, user_agent, x_vercel_cron_schedule, x_admin_password
    ):
        raise HTTPException(401, "Unauthorized")
    try:
        return {"ok": True, **scrape_ingest.run_all_enabled()}
    except Exception as exc:
        raise HTTPException(500, f"Scrape cron failed: {exc}") from exc


@app.get("/api/cron/weekly-intelligence")
def cron_weekly_intelligence(
    authorization: str = Header(default=None, alias="Authorization"),
    user_agent: str = Header(default=None, alias="User-Agent"),
    x_vercel_cron_schedule: str = Header(default=None, alias="X-Vercel-Cron-Schedule"),
    x_admin_password: str = Header(default=None, alias="X-Admin-Password"),
):
    """Monday cron: email subscribers the previous week's Intelligence Hub briefs."""
    if not _cron_allowed(
        authorization, user_agent, x_vercel_cron_schedule, x_admin_password
    ):
        raise HTTPException(401, "Unauthorized")
    if not database.using_turso():
        restore_db(database.DB_PATH)
    try:
        result = email_digest.send_weekly_intelligence_emails()
    except LookupError as exc:
        return {"ok": True, "sent_count": 0, "message": str(exc)}
    except Exception as exc:
        raise HTTPException(500, f"Weekly intelligence email failed: {exc}") from exc
    return {"ok": True, **result}


@app.get("/api/admin/storage-status")
def admin_storage_status(_: None = Depends(admin_auth.require_admin)):
    """Check durable storage (Turso preferred, else disk/Blob)."""
    # Probe the live connection so Turso status is not based on env alone.
    if database.using_turso():
        try:
            with database.get_conn() as conn:
                conn.execute("SELECT 1 AS ok").fetchone()
        except Exception as exc:
            db_conn.LAST_ERROR = str(exc)

    info = database.storage_info()
    status = {
        "backend": info["backend"],
        "durable": info["durable"],
        "label": info["label"],
        "hint": info["hint"],
        "database_path": database.DB_PATH,
        "persistent_disk": using_persistent_disk(),
        "turso": database.using_turso(),
        "relevant_articles": database.get_relevant_count(),
        "total_articles": database.get_stats()["total"],
        "blob_actions_enabled": False,
    }
    if database.using_turso():
        status["blob_configured"] = False
        status["blob_actions_enabled"] = False
        status["turso_host"] = info.get("turso_url_host")
        status["connected"] = info.get("connected")
        status["db_driver"] = info.get("db_driver") or db_conn.LAST_DRIVER
        status["db_error"] = info.get("db_error") or db_conn.LAST_ERROR
        if info.get("connected"):
            status["hint"] = (
                "Turso is already durable — Save/Restore Cloud (Blob) is disabled. "
                "Your Blob store was suspended; leave it off."
            )
        else:
            status["hint"] = info.get("hint") or (
                "Turso env is set but not connected — check /api/health and redeploy "
                "after libsql packages are installed."
            )
        return status

    blob = storage_status(database.DB_PATH)
    status.update(blob)
    status["blob_actions_enabled"] = bool(status.get("blob_configured"))
    if using_persistent_disk():
        status["hint"] = info["hint"]
        status["blob_actions_enabled"] = False
    elif status.get("blob_configured") and status.get("last_save_ok"):
        status["hint"] = "Blob save succeeded — but Turso is safer than Blob free limits."
    elif not status.get("blob_configured"):
        status["hint"] = info["hint"]
    return status


@app.get("/api/admin/scrape/status")
def admin_scrape_status(_: None = Depends(admin_auth.require_admin)):
    return scrape_ingest.status_summary()


@app.get("/api/admin/scrape/targets")
def admin_list_scrape_targets(
    kind: str = None, _: None = Depends(admin_auth.require_admin)
):
    return scrape_ingest.list_targets(kind=kind)


@app.post("/api/admin/scrape/targets")
async def admin_add_scrape_target(
    request: Request, _: None = Depends(admin_auth.require_admin)
):
    body = await request.json()
    try:
        target = scrape_ingest.add_target(
            kind=body.get("kind"),
            url=body.get("url") or body.get("handle") or "",
            label=body.get("label"),
            sector=body.get("sector") or "festive_sale",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "target": target}


@app.delete("/api/admin/scrape/targets/{target_id}")
def admin_delete_scrape_target(target_id: int, _: None = Depends(admin_auth.require_admin)):
    if not scrape_ingest.delete_target(target_id):
        raise HTTPException(404, "Target not found")
    return {"ok": True}


@app.post("/api/admin/scrape/targets/{target_id}/run")
def admin_run_scrape_target(target_id: int, _: None = Depends(admin_auth.require_admin)):
    try:
        return scrape_ingest.run_target(target_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@app.post("/api/admin/scrape/run")
async def admin_run_scrape_all(
    request: Request, _: None = Depends(admin_auth.require_admin)
):
    kind = None
    try:
        body = await request.json()
        kind = (body or {}).get("kind")
    except Exception:
        kind = None
    return scrape_ingest.run_all_enabled(kind=kind)


@app.post("/api/admin/scrape/seed-defaults")
def admin_seed_scrape_defaults(_: None = Depends(admin_auth.require_admin)):
    """Load curated research firms, newsrooms, and Instagram handles."""
    return scrape_ingest.seed_default_targets()


@app.post("/api/admin/restore-db")
def admin_restore_db(_: None = Depends(admin_auth.require_admin)):
    """Restore SQLite from Vercel Blob (legacy). Not needed when Turso is configured."""
    if database.using_turso():
        count = database.get_relevant_count()
        return {
            "ok": True,
            "skipped": True,
            "turso": True,
            "message": (
                "Already on Turso — no Blob restore needed. "
                f"Your {count} articles are already in the durable Turso database."
            ),
            "relevant_articles": count,
        }
    if not enabled():
        raise HTTPException(
            400,
            "Blob is not available. Prefer Turso: set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN on Vercel. "
            "Blob free limits already suspended this project's store.",
        )
    ok = restore_db(database.DB_PATH, force=True)
    database.init_db()
    status = storage_status(database.DB_PATH)
    if not ok and not status.get("local_db_bytes", 0):
        err = status.get("last_save_error") or ""
        if "store_suspended" in err.lower() or "suspended" in err.lower():
            raise HTTPException(
                400,
                "Vercel Blob store is suspended (free limit). "
                "Use Turso instead — do not click Restore from Cloud.",
            )
        raise HTTPException(
            400,
            err or "Could not restore from Blob. Prefer Turso durable DB.",
        )
    return {
        "ok": ok,
        **status,
        "message": "Database restored from Vercel Blob." if ok else "Restore skipped or blob empty.",
        "relevant_articles": database.get_relevant_count(),
    }


@app.post("/api/admin/persist-db")
def admin_persist_db(force: bool = False, _: None = Depends(admin_auth.require_admin)):
    """Force-save the database to Vercel Blob (legacy). Not needed when Turso is configured."""
    if database.using_turso():
        count = database.get_relevant_count()
        return {
            "ok": True,
            "skipped": True,
            "turso": True,
            "message": (
                "Already on Turso — every write is already durable. "
                "Save to Cloud (Blob) is disabled because the Blob store was suspended."
            ),
            "relevant_articles": count,
        }
    if not enabled():
        raise HTTPException(
            400,
            "Blob is not available. Prefer Turso: set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN on Vercel.",
        )
    ok = save_db(database.DB_PATH, force=force)
    status = storage_status(database.DB_PATH)
    if not ok:
        err = status.get("last_save_error") or "Blob save failed"
        if "store_suspended" in str(err).lower() or "suspended" in str(err).lower():
            raise HTTPException(
                400,
                "Vercel Blob store is suspended (free limit hit earlier). "
                "Switch to Turso — do not use Save to Cloud.",
            )
        raise HTTPException(400, err)
    return {"ok": True, **status, "message": "Database saved to Vercel Blob."}


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
    database.persist()

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
    try:
        articles, stats = rss_ingest.fetch_all()
        inserted, new_ids, refreshed_ids = database.insert_articles(articles)
        dedupe_stats = dedupe.run_all_dedupes()
        merged = dedupe_stats["total_merged"]
        errors = stats.get("errors") or []
        feeds_ok = stats.get("feeds_ok", 0)
        feeds_total = stats.get("feeds_total", 0)
        max_per_feed = stats.get("max_items_per_feed", 50)
        msg = (
            f"Scanned {stats['fetched']} headlines from {feeds_ok}/{feeds_total} feeds "
            f"(up to {max_per_feed} per feed). "
            f"{stats['matched']} matched your keywords — "
            f"{inserted} new, {len(refreshed_ids)} updated."
        )
        if errors:
            msg += f" {len(errors)} feed(s) failed: {errors[0]}"
        database.persist()
        return {
            "fetched": stats["fetched"],
            "matched": stats["matched"],
            "skipped": stats["skipped"],
            "feeds_ok": feeds_ok,
            "feeds_total": feeds_total,
            "max_items_per_feed": max_per_feed,
            "inserted": inserted,
            "refreshed": len(refreshed_ids),
            "merged_duplicates": merged,
            "new_ids": new_ids,
            "refreshed_ids": refreshed_ids,
            "highlight_ids": new_ids + refreshed_ids,
            "keyword_filter": stats["keyword_filter"],
            "errors": errors,
            "feed_stats": stats.get("feed_stats") or [],
            "message": msg,
        }
    except Exception as exc:
        raise HTTPException(500, f"RSS refresh failed: {exc}") from exc


@app.post("/api/refresh-newsapi")
def refresh_newsapi(_: None = Depends(admin_auth.require_admin)):
    api_key = os.environ.get("NEWSAPIKEY") or os.environ.get("NEWSAPI_KEY")
    if not api_key:
        raise HTTPException(400, "NEWSAPIKEY is not set on the server")
    try:
        articles, stats = newsapi_ingest.fetch_all()
        inserted, new_ids, refreshed_ids = database.insert_articles(articles)
        dedupe_stats = dedupe.run_all_dedupes()
        merged = dedupe_stats["total_merged"]
        errors = stats.get("errors") or []
        endpoint = stats.get("endpoint", "top-headlines")
        msg = (
            f"NewsAPI ({endpoint}) saved {inserted} new, {len(refreshed_ids)} updated "
            f"({stats.get('raw_from_api', 0)} raw from API)."
        )
        if not articles and errors:
            msg = f"NewsAPI returned no articles. {'; '.join(errors[:2])}"
        elif errors:
            msg += f" {len(errors)} sector query had errors."
        database.persist()
        return {
            "fetched": stats["fetched"],
            "raw_from_api": stats.get("raw_from_api", stats["fetched"]),
            "matched": stats["matched"],
            "skipped": stats["skipped"],
            "inserted": inserted,
            "refreshed": len(refreshed_ids),
            "merged_duplicates": merged,
            "new_ids": new_ids,
            "refreshed_ids": refreshed_ids,
            "highlight_ids": new_ids + refreshed_ids,
            "errors": errors,
            "endpoint": endpoint,
            "message": msg,
        }
    except Exception as exc:
        raise HTTPException(500, f"NewsAPI refresh failed: {exc}") from exc


@app.post("/api/refresh-all")
def refresh_all_sources(_: None = Depends(admin_auth.require_admin)):
    """Refresh RSS + Google News RSS + GNews + NewsAPI in one go."""
    try:
        summary = auto_refresh.run_refresh(include_rss=True, include_gnews=True, include_newsapi=True)
        inserted = summary.get("total_inserted", 0)
        refreshed = summary.get("total_refreshed", 0)
        msg = f"All sources refreshed — {inserted} new, {refreshed} updated."
        return {"ok": True, "message": msg, **summary}
    except Exception as exc:
        raise HTTPException(500, f"Refresh all failed: {exc}") from exc


@app.post("/api/refresh-gnews")
def refresh_gnews(_: None = Depends(admin_auth.require_admin)):
    if not (os.environ.get("GNEWSAPIKEY") or os.environ.get("GNEWS_API_KEY")):
        raise HTTPException(400, "GNEWSAPIKEY is not set on the server")
    try:
        articles, stats = gnews_ingest.fetch_all()
        inserted, new_ids, refreshed_ids = database.insert_articles(articles)
        dedupe_stats = dedupe.run_all_dedupes()
        merged = dedupe_stats["total_merged"]
        errors = stats.get("errors") or []
        raw_from_api = stats.get("raw_from_api", stats["fetched"])
        sector_count = len(GNEWS_QUERIES)
        sectors_ok = sector_count - len(errors)
        msg = (
            f"GNews saved {inserted} new, {len(refreshed_ids)} updated "
            f"({sectors_ok}/{sector_count} sectors)."
        )
        if not articles and raw_from_api == 0 and errors:
            msg = f"GNews returned no articles. {'; '.join(errors[:3])}"
        elif not articles and raw_from_api > 0:
            msg = (
                f"GNews returned {raw_from_api} headline(s) but none matched sector keywords."
            )
        elif errors:
            rate_hits = sum(1 for err in errors if "rate limit" in err.lower() or "429" in err)
            quota_hits = sum(1 for err in errors if "quota" in err.lower() or "403" in err)
            if rate_hits:
                msg += (
                    " Some sectors were skipped because GNews free plan allows only 1 request per second."
                )
            elif quota_hits:
                msg += " Daily GNews quota used — resets at midnight UTC."
            else:
                msg += f" {len(errors)} sector query failed."
        database.persist()
        return {
            "fetched": stats["fetched"],
            "raw_from_api": raw_from_api,
            "matched": stats["matched"],
            "skipped": stats["skipped"],
            "inserted": inserted,
            "refreshed": len(refreshed_ids),
            "merged_duplicates": merged,
            "new_ids": new_ids,
            "refreshed_ids": refreshed_ids,
            "highlight_ids": new_ids + refreshed_ids,
            "errors": errors,
            "message": msg,
        }
    except Exception as exc:
        raise HTTPException(500, f"GNews refresh failed: {exc}") from exc


@app.post("/api/process")
def process_unprocessed(_: None = Depends(admin_auth.require_admin)):
    """Classify/tag/summarize all unprocessed articles via Gemini, then dedupe."""
    if not (os.environ.get("GEMINIAPIKEY") or os.environ.get("GEMINI_API_KEY")):
        raise HTTPException(400, "GEMINIAPIKEY is not set on the server")

    try:
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

        batch_opts = classify_and_summarize.batch_settings()
        results, stats = classify_and_summarize.classify_batch(
            process_rows,
            batch_size=batch_opts["batch_size"],
            pause_between_calls=batch_opts["pause_between_calls"],
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
            extra = f"{stats['batches_failed']} batch(es) skipped — Gemini quota or API error."
            message = f"{message}. {extra}" if message else extra
        remaining = total_pending - len(process_rows)
        if remaining > 0:
            extra = (
                f"{remaining} still pending — click Process with Gemini again "
                f"(Vercel processes up to {settings['limit'] or 15} articles per click)."
            )
            message = f"{message}. {extra}" if message else extra
        if not results and process_rows:
            message = (
                message or "Gemini could not classify any articles in this batch. "
                "Check GEMINIAPIKEY and try again."
            )

        database.persist()
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
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Gemini processing failed: {exc}") from exc


@app.post("/api/dedupe-all")
def dedupe_all(_: None = Depends(admin_auth.require_admin)):
    """Remove duplicate stories already stored (same URL, headline, or same event)."""
    result = dedupe.run_all_dedupes()
    database.persist()
    return {
        **result,
        "message": f"Removed {result['total_merged']} duplicate(s).",
    }


@app.post("/api/dedupe-recent")
def dedupe_recent():
    """Fast same-story merge for recent Google News copies (safe on page load)."""
    result = {
        "url_merged": database.dedupe_by_url(),
        "title_merged": database.dedupe_by_title(),
        "fuzzy_merged": dedupe.find_and_mark_story_duplicates(days=21),
    }
    result["total_merged"] = (
        result["url_merged"] + result["title_merged"] + result["fuzzy_merged"]
    )
    if result["total_merged"]:
        database.persist()
    return {
        **result,
        "message": (
            f"Merged {result['total_merged']} duplicate stor{'y' if result['total_merged'] == 1 else 'ies'}."
            if result["total_merged"]
            else "No duplicate stories found."
        ),
    }


@app.post("/api/send-email")
async def send_email_digest(
    request: Request,
    pub_date: str = None,
    _: None = Depends(admin_auth.require_admin),
):
    """Send digest emails to active subscribers for selected sector(s) and dates."""
    if not pub_date:
        pub_date = str(date.today())

    sectors = None
    start_date = None
    end_date = None
    try:
        body = await request.json()
        sectors = body.get("sectors")
        start_date = body.get("start_date")
        end_date = body.get("end_date")
    except Exception:
        sectors = None

    try:
        payload = email_digest.send_sector_digests(
            pub_date,
            sectors=sectors,
            start_date=start_date,
            end_date=end_date,
        )
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))
    except smtplib.SMTPException as e:
        raise HTTPException(500, f"Email failed: {e}")

    mode = payload.get("mode", "legacy")
    period = payload.get("pub_date") or pub_date
    if mode == "subscribers":
        sent = [r for r in payload.get("results", []) if r.get("status") == "sent"]
        skipped = [r for r in payload.get("results", []) if r.get("status") == "skipped"]
        return {
            "pub_date": period,
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "mode": mode,
            "sent_count": len(sent),
            "skipped_count": len(skipped),
            "sectors": payload.get("sectors", []),
            "results": payload.get("results", []),
            "message": f"Sent {len(sent)} email(s) to subscribers for {period}.",
        }

    sent = [r for r in payload.get("results", []) if r.get("status") == "sent"]
    skipped = [r for r in payload.get("results", []) if r.get("status") == "skipped"]
    return {
        "pub_date": period,
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "mode": mode,
        "sent_count": len(sent),
        "skipped_count": len(skipped),
        "sectors": payload.get("sectors", []),
        "results": payload.get("results", []),
        "message": f"Sent {len(sent)} sector email(s) for {period}.",
    }


def _refresh_one_sector(sector):
    """Fetch only one sector (Google News RSS + GNews) so it can finish under 60s."""
    if sector not in GNEWS_QUERIES:
        raise HTTPException(404, f"Unknown sector '{sector}'")

    rss_query = GOOGLE_NEWS_QUERIES.get(sector) or f"({GNEWS_QUERIES[sector]}) India when:7d"
    feeds = [(f"Google News — {sector}", rss_ingest._google_news_url(rss_query))]
    articles, rss_stats = rss_ingest.fetch_all(feeds=feeds)
    gnews_articles, gnews_stats = [], {}
    if os.environ.get("GNEWSAPIKEY") or os.environ.get("GNEWS_API_KEY"):
        gnews_articles, gnews_stats = gnews_ingest.fetch_all(
            queries={sector: GNEWS_QUERIES[sector]}
        )
    combined = articles + gnews_articles
    inserted, new_ids, refreshed_ids = database.insert_articles(combined)
    dedupe.run_all_dedupes()
    database.persist()
    return {
        "sector": sector,
        "inserted": inserted,
        "refreshed": len(refreshed_ids),
        "new_ids": new_ids,
        "rss_matched": rss_stats.get("matched", len(articles)),
        "gnews_matched": gnews_stats.get("matched", 0),
        "errors": (rss_stats.get("errors") or []) + (gnews_stats.get("errors") or []),
    }


@app.post("/api/backfill-new-sectors")
def backfill_new_sectors(_: None = Depends(admin_auth.require_admin)):
    """Tag already-stored news for Chocolate / Media / Festive Sale."""
    if database.get_meta("new_sectors_backfill_v3") == "1":
        return {
            "skipped": True,
            "message": "Chocolate, Media, and Festive Sale tags already backfilled.",
        }
    chocolate = database.reconcile_taxonomy_tags("chocolate", remove_unmatched=False)
    media = database.reconcile_taxonomy_tags(
        "media_entertainment", remove_unmatched=True
    )
    festive = database.reconcile_festive_sale_tags()
    database.set_meta("new_sectors_backfill_v3", "1")
    database.persist()
    return {
        "chocolate": chocolate,
        "media_entertainment": media,
        "festive_sale": festive,
        "skipped": False,
        "message": (
            f"Chocolate: {chocolate['added']} tagged. "
            f"Media & Entertainment: {media['added']} tagged, {media['removed']} removed. "
            f"Festive Sale: {festive['added']} tagged."
        ),
    }


@app.post("/api/refresh-sector/{sector}")
def refresh_one_sector(sector: str, _: None = Depends(admin_auth.require_admin)):
    """Lightweight single-sector fetch for Chocolate (or any other sector)."""
    if sector == "chocolate":
        last = database.get_meta("chocolate_sector_fetch_at")
        if last:
            try:
                parsed = datetime.fromisoformat(last.replace("Z", ""))
                if datetime.utcnow() - parsed < timedelta(hours=12):
                    return {
                        "sector": sector,
                        "skipped": True,
                        "inserted": 0,
                        "refreshed": 0,
                        "new_ids": [],
                        "message": "Chocolate news already fetched recently.",
                    }
            except ValueError:
                pass
    try:
        result = _refresh_one_sector(sector)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Sector refresh failed: {exc}") from exc
    if sector == "chocolate":
        database.set_meta(
            "chocolate_sector_fetch_at",
            datetime.utcnow().isoformat(timespec="seconds") + "Z",
        )
        database.persist()
    result["skipped"] = False
    result["message"] = (
        f"{SECTOR_LABELS.get(sector, sector)}: {result['inserted']} new, "
        f"{result['refreshed']} updated."
    )
    return result


@app.post("/api/reconcile-ride-hailing")
def reconcile_ride_hailing(_: None = Depends(admin_auth.require_admin)):
    """Remove wrongly tagged ride-hailing articles and add newly matching ones."""
    result = database.reconcile_ride_hailing_tags()
    database.persist()
    return {
        **result,
        "message": (
            f"Ride hailing refilter done: {result['removed']} removed, "
            f"{result['added']} added."
        ),
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
