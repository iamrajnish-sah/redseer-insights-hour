"""
festive_jobs.py

Automation helpers used by cron:
  - festive-only Gemini summarization (hourly)
  - festive intelligence brief generation + email (every 4–5 hours)
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import classify_and_summarize
import database
import email_digest
import intelligence_hub
from sector_keywords import is_festive_sale_relevant, normalize_sector_tags


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def festive_candidate_rows(limit=12):
    """Unprocessed articles that look festive — only these get hourly Gemini."""
    limit = max(1, min(int(limit or 12), 40))
    rows, settings, _, _ = classify_and_summarize.get_rows_for_processing()
    festive = []
    for row in rows:
        title = row["title"] or ""
        body = row["body"] or ""
        subtitle = row["subtitle"] or ""
        origin = (row["origin"] or "").lower()
        # Instagram + LinkedIn are hub-only; website scrape + festive news go into summaries/insights.
        if origin in ("instagram", "linkedin_scrape"):
            continue
        if origin in ("website_scrape", "festive_sale"):
            festive.append(row)
            continue
        if is_festive_sale_relevant(title, body, subtitle):
            festive.append(row)
            continue
        try:
            sectors = json.loads(row["sectors"] or "[]")
        except (TypeError, json.JSONDecodeError):
            sectors = []
        if "festive_sale" in (sectors or []):
            festive.append(row)
        if len(festive) >= limit:
            break
    return festive[:limit], settings


def summarize_festive_only(limit=None):
    """Run Gemini only on festive-sale candidates. Other sectors are skipped."""
    if not (os.environ.get("GEMINIAPIKEY") or os.environ.get("GEMINI_API_KEY")):
        return {"classified": 0, "skipped": True, "reason": "no_gemini_key"}

    limit = int(limit or os.environ.get("HOURLY_FESTIVE_GEMINI_LIMIT", "12"))
    rows, settings = festive_candidate_rows(limit=limit)
    if not rows:
        return {"classified": 0, "candidates": 0, "message": "No festive articles pending summary."}

    batch_opts = classify_and_summarize.batch_settings()
    results, stats = classify_and_summarize.classify_batch(
        rows,
        batch_size=batch_opts["batch_size"],
        pause_between_calls=batch_opts["pause_between_calls"],
        body_chars=settings["body_chars"],
    )
    classified = 0
    for row in rows:
        result = results.get(row["id"])
        if not result:
            continue
        sectors = normalize_sector_tags(result["sectors"] or [])
        # Keep festive signal even if model omits it for a clear sale story
        if "festive_sale" not in sectors and is_festive_sale_relevant(
            row["title"] or "", row["body"] or "", row["subtitle"] or ""
        ):
            sectors.insert(0, "festive_sale")
        database.update_classification(
            row["id"],
            result["is_relevant"] or ("festive_sale" in sectors),
            sectors,
            result["summary"],
        )
        classified += 1
    if classified:
        database.persist()
    return {
        "classified": classified,
        "candidates": len(rows),
        "stats": stats,
        "message": f"Summarized {classified} festive article(s).",
    }


def festive_period_dates(period="daily", hours=None):
    """Calendar window for festive intelligence briefs."""
    period = (period or "daily").strip().lower()
    today = date.today()
    if period in ("weekly", "week"):
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        return start.isoformat(), end.isoformat()
    # daily (default)
    iso = today.isoformat()
    return iso, iso


def generate_festive_intelligence(force=True, generated_by="cron", period="daily"):
    if not intelligence_hub.intelligence_configured():
        return {
            "ok": False,
            "skipped": True,
            "reason": "INTELLIGENCE_GEMINI_API_KEY not configured",
        }
    start_date, end_date = festive_period_dates(period=period)
    try:
        report = intelligence_hub.generate_or_get_report(
            "festive_sale",
            start_date,
            end_date,
            force=force,
            generated_by=generated_by,
        )
    except LookupError as exc:
        return {"ok": False, "skipped": True, "reason": str(exc), "start_date": start_date}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "start_date": start_date}
    return {
        "ok": True,
        "report_id": report.get("id"),
        "cached": report.get("cached"),
        "article_count": report.get("article_count"),
        "start_date": start_date,
        "end_date": end_date,
        "period": period,
        "period_label": report.get("period_label"),
    }


def email_festive_intelligence(report=None, period="daily"):
    """Email festive_sale brief to subscribers / sector recipient list."""
    if not email_digest.smtp_configured():
        return {"ok": False, "skipped": True, "reason": "SMTP not configured"}

    start_date, end_date = festive_period_dates(period=period)
    if report is None:
        report = intelligence_hub.get_cached_report("festive_sale", start_date, end_date)
    if not report:
        overlapping = intelligence_hub.reports_for_period("festive_sale", start_date, end_date)
        report = overlapping[0] if overlapping else None
    if not report:
        return {"ok": False, "skipped": True, "reason": "No festive intelligence report to email"}

    enriched = intelligence_hub.enrich_report_with_sources(report)
    period_label = enriched.get("period_label") or f"{start_date}"

    # Prefer subscribers who follow festive_sale; fall back to sector_recipients.json
    import subscribers

    active = subscribers.list_subscribers(status=subscribers.STATUS_SUBSCRIBED)
    emails = []
    for sub in active:
        if "festive_sale" in (sub.get("sectors") or []):
            emails.append({"email": sub["email"], "name": sub.get("name"), "token": sub["token"]})

    if not emails:
        recipients = email_digest.load_recipients().get("festive_sale") or []
        for addr in recipients:
            emails.append({"email": addr, "name": None, "token": None})

    if not emails:
        return {
            "ok": False,
            "skipped": True,
            "reason": "No festive_sale subscribers or sector recipients configured",
        }

    card = email_digest._brief_card_html(enriched)
    sent = []
    for item in emails:
        footer = ""
        if item.get("token"):
            footer = email_digest.build_email_footer(item["token"])
        html = email_digest._wrap_email_html(
            header_title=f"{email_digest.PRODUCT_NAME} — Festive Sale Intelligence",
            header_subtitle=period_label,
            body_rows=card,
            footer_html=footer,
        )
        subject = f"{email_digest.PRODUCT_NAME} — Festive Sale — {period_label}"
        email_digest._send_email([item["email"]], subject, html)
        sent.append(item["email"])

    return {
        "ok": True,
        "sent_count": len(sent),
        "emails": sent,
        "report_id": enriched.get("id"),
        "period_label": period_label,
    }


def run_festive_intelligence_and_email(force=True, period="daily"):
    gen = generate_festive_intelligence(force=force, generated_by="cron-festive", period=period)
    mail = {"ok": False, "skipped": True, "reason": "generation skipped"}
    if gen.get("ok"):
        mail = email_festive_intelligence(period=period)
    return {"generation": gen, "email": mail}
